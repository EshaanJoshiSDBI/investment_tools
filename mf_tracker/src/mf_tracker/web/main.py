from __future__ import annotations

import csv
import io
import os
import tempfile
from datetime import date
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

from ..bundles import export_bundle, verify_archive
from ..comparison import classify_movement, compare_snapshots, movement_value
from ..domain import MetadataOverrides
from ..errors import MfTrackerError, SnapshotConflictError
from ..ingestion import ingest_file
from ..persistence import SCHEMA_VERSION, SQLiteRepository, SourceArchive

MAX_UPLOAD_BYTES = 25 * 1024 * 1024
FRONTEND_DIR = Path(__file__).resolve().parents[3] / "frontend"


def _error(status: int, code: str, message: str, details: Any = None) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message, "details": details})


def _metadata(report_date: str | None, fund_code: str | None, fund_name: str | None, amc_name: str | None) -> MetadataOverrides:
    try:
        parsed_date = date.fromisoformat(report_date) if report_date else None
    except ValueError as exc:
        raise _error(422, "invalid_report_date", "Report date must use YYYY-MM-DD.") from exc
    return MetadataOverrides(parsed_date, fund_code, fund_name, amc_name)


async def _store_upload(upload: UploadFile) -> Path:
    directory = Path(tempfile.mkdtemp(prefix="mf-tracker-"))
    filename = Path(upload.filename or "workbook.xlsx").name
    path = directory / filename
    size = 0
    try:
        with path.open("wb") as handle:
            while chunk := await upload.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise _error(413, "upload_too_large", "Workbook cannot exceed 25 MiB.")
                handle.write(chunk)
        return path
    except Exception:
        path.unlink(missing_ok=True)
        directory.rmdir()
        raise


def _csv_response(rows: list[dict[str, Any]], filename: str) -> Response:
    output = io.StringIO()
    columns = list(rows[0]) if rows else []
    writer = csv.DictWriter(output, fieldnames=columns)
    if columns:
        writer.writeheader()
        writer.writerows(rows)
    return Response(output.getvalue(), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{filename}"'})


TIMELINE_PERIODS = {"6m": 6, "12m": 12, "24m": 24, "all": None}
CHANGE_TYPES = ("introduced", "increased", "decreased", "exited", "unchanged")


def _delta(before: dict[str, Any] | None, after: dict[str, Any] | None, field: str) -> float | None:
    before_value = before.get(field) if before else None
    after_value = after.get(field) if after else None
    if before_value is None or after_value is None:
        return None
    return after_value - before_value


def _timeline_payload(
    repo: SQLiteRepository,
    fund_id: int,
    period: str,
    focus_from: str | None,
    focus_to: str | None,
    search: str,
    asset_class: str,
    change_type: str,
) -> dict[str, Any]:
    if period not in TIMELINE_PERIODS:
        raise _error(422, "invalid_period", "Period must be one of 6m, 12m, 24m, or all.")
    if change_type and change_type not in CHANGE_TYPES:
        raise _error(422, "invalid_change_type", "Focused movement filter is not supported.")

    snapshots_desc = repo.list_snapshots(fund_id)
    if not snapshots_desc:
        raise _error(404, "fund_snapshots_not_found", "No active disclosures were found for this fund.")
    limit = TIMELINE_PERIODS[period]
    selected_desc = snapshots_desc if limit is None else snapshots_desc[:limit]
    selected = list(reversed(selected_desc))
    visible_dates = [item["report_date"] for item in selected]
    available_dates = [item["report_date"] for item in snapshots_desc]

    if len(visible_dates) >= 2:
        focus_from = focus_from or visible_dates[-2]
        focus_to = focus_to or visible_dates[-1]
    elif not focus_from and not focus_to:
        focus_from = focus_to = visible_dates[0]
    try:
        if focus_from:
            date.fromisoformat(focus_from)
        if focus_to:
            date.fromisoformat(focus_to)
    except ValueError as exc:
        raise _error(422, "invalid_date", "Timeline dates must use YYYY-MM-DD.") from exc
    if focus_from not in visible_dates or focus_to not in visible_dates:
        raise _error(422, "focus_outside_period", "Focused dates must be visible in the selected period.")
    if focus_from > focus_to:
        raise _error(422, "invalid_focus_interval", "Focused from-date must not follow the to-date.")

    oldest_index = available_dates.index(visible_dates[0])
    preceding_date = available_dates[oldest_index + 1] if oldest_index + 1 < len(available_dates) else None
    query_dates = ([preceding_date] if preceding_date else []) + visible_dates
    raw_rows = repo.timeline_rows(fund_id, query_dates)
    by_identity: dict[str, dict[str, dict[str, Any]]] = {}
    for row in raw_rows:
        report_date = row.pop("report_date")
        by_identity.setdefault(row["identity_key"], {})[report_date] = row

    rows: list[dict[str, Any]] = []
    for identity_key, history in by_identity.items():
        visible_holdings = [history.get(value) for value in visible_dates]
        metadata = next((item for item in reversed(visible_holdings) if item is not None), None)
        if metadata is None:
            continue
        focus_before = history.get(focus_from)
        focus_after = history.get(focus_to)
        focus_asset = (focus_after or focus_before or metadata)["asset_class"]
        # A holding can belong to the wider timeline without existing at either
        # focused endpoint. Keep it visible historically, but do not mislabel it
        # as unchanged or include it in the focused comparison counts.
        focused_change = (
            classify_movement(focus_asset, focus_before, focus_after)
            if focus_before is not None or focus_after is not None
            else None
        )

        points: list[dict[str, Any]] = []
        previous = history.get(preceding_date) if preceding_date else None
        for point_index, (report_date, holding) in enumerate(zip(visible_dates, visible_holdings, strict=True)):
            point_asset = (holding or previous or metadata)["asset_class"]
            has_previous_snapshot = preceding_date is not None or point_index > 0
            action = classify_movement(point_asset, previous, holding) if has_previous_snapshot and (previous is not None or holding is not None) else None
            movement_before = movement_value(point_asset, previous)
            movement_after = movement_value(point_asset, holding)
            action_delta = None if movement_before is None or movement_after is None else movement_after - movement_before
            points.append({
                "report_date": report_date,
                "present": holding is not None,
                "quantity": holding.get("quantity") if holding else None,
                "market_value_lakh": holding.get("market_value_lakh") if holding else None,
                "weight": holding.get("weight") if holding else None,
                "action": action,
                "action_metric": "market_value_lakh" if point_asset in {"cash_receivable", "repo_treps"} else "quantity",
                "action_delta": action_delta,
            })
            previous = holding

        row = {
            "identity_key": identity_key,
            "display_name": metadata["display_name"],
            "isin": metadata.get("isin"),
            "asset_class": metadata["asset_class"],
            "instrument_type": metadata["instrument_type"],
            "industry_rating": metadata.get("industry_rating"),
            "section": metadata.get("section"),
            "subsection": metadata.get("subsection"),
            "focus": {
                "change_type": focused_change,
                "quantity_from": focus_before.get("quantity") if focus_before else None,
                "quantity_to": focus_after.get("quantity") if focus_after else None,
                "quantity_delta": _delta(focus_before, focus_after, "quantity"),
                "market_value_from": focus_before.get("market_value_lakh") if focus_before else None,
                "market_value_to": focus_after.get("market_value_lakh") if focus_after else None,
                "market_value_delta": _delta(focus_before, focus_after, "market_value_lakh"),
                "weight_from": focus_before.get("weight") if focus_before else None,
                "weight_to": focus_after.get("weight") if focus_after else None,
                "weight_delta": _delta(focus_before, focus_after, "weight"),
            },
            "points": points,
        }
        rows.append(row)

    asset_classes = sorted({row["asset_class"] for row in rows})
    needle = search.strip().casefold()
    filtered_base = [row for row in rows if not needle or needle in row["display_name"].casefold() or needle in str(row.get("isin") or "").casefold()]
    if asset_class:
        filtered_base = [row for row in filtered_base if row["asset_class"] == asset_class]
    counts = {kind: sum(row["focus"]["change_type"] == kind for row in filtered_base) for kind in CHANGE_TYPES}
    filtered = [row for row in filtered_base if not change_type or row["focus"]["change_type"] == change_type]
    filtered.sort(key=lambda row: (row["points"][-1]["weight"] is None, -(row["points"][-1]["weight"] or 0), row["display_name"].casefold()))
    return {
        "dates": selected,
        "period": period,
        "preceding_date": preceding_date,
        "focus": {"from_date": focus_from, "to_date": focus_to, "counts": counts},
        "asset_classes": asset_classes,
        "total": len(filtered),
        "items": filtered,
    }


def _timeline_csv_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    dates = [item["report_date"] for item in payload["dates"]]
    for item in payload["items"]:
        focus = item["focus"]
        row = {
            "identity_key": item["identity_key"],
            "instrument": item["display_name"],
            "isin": item["isin"],
            "asset_class": item["asset_class"],
            "instrument_type": item["instrument_type"],
            "focused_change": focus["change_type"],
            "focused_quantity_delta": focus["quantity_delta"],
            "focused_weight_delta_pct": None if focus["weight_delta"] is None else focus["weight_delta"] * 100,
        }
        for report_date, point in zip(dates, item["points"], strict=True):
            row[f"{report_date}_weight_pct"] = None if point["weight"] is None else point["weight"] * 100
            row[f"{report_date}_quantity"] = point["quantity"]
            row[f"{report_date}_market_value_lakh"] = point["market_value_lakh"]
            row[f"{report_date}_action"] = point["action"]
            row[f"{report_date}_action_delta"] = point["action_delta"]
        rows.append(row)
    return rows


def create_app(db_path: str | Path, source_store: str | Path | None = None) -> FastAPI:
    database = Path(db_path)
    archive = SourceArchive(source_store or f"{database}.sources")
    app = FastAPI(title="MF Tracker", docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.state.db_path = database
    app.state.archive = archive

    def repository() -> SQLiteRepository:
        return SQLiteRepository(app.state.db_path, source_archive=app.state.archive)

    @app.exception_handler(MfTrackerError)
    async def tracker_error(_, exc: MfTrackerError) -> JSONResponse:
        code = "snapshot_conflict" if isinstance(exc, SnapshotConflictError) else "tracker_error"
        status = 409 if isinstance(exc, SnapshotConflictError) else 400
        return JSONResponse(status_code=status, content={"error": {"code": code, "message": str(exc), "details": None}})

    @app.exception_handler(HTTPException)
    async def http_error(_, exc: HTTPException) -> JSONResponse:
        detail = exc.detail if isinstance(exc.detail, dict) else {"code": "request_error", "message": str(exc.detail), "details": None}
        return JSONResponse(status_code=exc.status_code, content={"error": detail})

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        with repository() as repo:
            repo.connection.execute("SELECT 1").fetchone()
        return {"status": "ok", "schema_version": SCHEMA_VERSION, "database": database.name, "archive": str(archive.root)}

    @app.get("/api/meta")
    def meta() -> dict[str, Any]:
        return {"supported_amcs": ["auto", "ppfas", "helios", "oldbridge"], "change_types": ["introduced", "exited", "increased", "decreased", "unchanged"], "currency_unit": "INR lakh"}

    @app.get("/api/overview")
    def overview(amc: str | None = None, months: int = Query(6, ge=1, le=24)) -> dict[str, Any]:
        with repository() as repo:
            result = repo.overview(months, amc)
            result["recent_imports"] = repo.list_imports(1, 5)["items"]
            return result

    @app.get("/api/amcs")
    def amcs() -> list[dict[str, Any]]:
        with repository() as repo:
            return repo.list_amcs()

    @app.get("/api/funds")
    def funds(amc: str | None = None) -> list[dict[str, Any]]:
        with repository() as repo:
            return repo.list_funds(amc)

    @app.get("/api/funds/{fund_id}/snapshots")
    def snapshots(fund_id: int, include_superseded: bool = False) -> list[dict[str, Any]]:
        with repository() as repo:
            return repo.list_snapshots(fund_id, include_superseded=include_superseded)

    @app.get("/api/funds/{fund_id}/timeline")
    def timeline(
        fund_id: int,
        period: str = "6m",
        focus_from: str | None = None,
        focus_to: str | None = None,
        search: str = "",
        asset_class: str = "",
        change_type: str = "",
    ) -> dict[str, Any]:
        with repository() as repo:
            return _timeline_payload(repo, fund_id, period, focus_from, focus_to, search, asset_class, change_type)

    @app.get("/api/funds/{fund_id}/timeline.csv")
    def timeline_csv(
        fund_id: int,
        period: str = "6m",
        focus_from: str | None = None,
        focus_to: str | None = None,
        search: str = "",
        asset_class: str = "",
        change_type: str = "",
    ) -> Response:
        with repository() as repo:
            payload = _timeline_payload(repo, fund_id, period, focus_from, focus_to, search, asset_class, change_type)
        filename = f"timeline-{fund_id}-{payload['dates'][0]['report_date']}-{payload['dates'][-1]['report_date']}.csv"
        return _csv_response(_timeline_csv_rows(payload), filename)

    @app.get("/api/funds/{fund_id}/holdings")
    def holdings(fund_id: int, report_date: str, search: str = "", asset_class: str = "", sort: str = "weight", direction: str = "desc", page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=250)) -> dict[str, Any]:
        with repository() as repo:
            return repo.query_holdings(fund_id, report_date, search=search, asset_class=asset_class, sort=sort, direction=direction, page=page, page_size=page_size)

    @app.get("/api/funds/{fund_id}/holdings.csv")
    def holdings_csv(fund_id: int, report_date: str, search: str = "", asset_class: str = "") -> Response:
        with repository() as repo:
            result = repo.query_holdings(fund_id, report_date, search=search, asset_class=asset_class, page_size=250)
            if result["total"] > len(result["items"]):
                result = repo.query_holdings(fund_id, report_date, search=search, asset_class=asset_class, page_size=result["total"])
            return _csv_response(result["items"], f"holdings-{report_date}.csv")

    def comparison_data(repo: SQLiteRepository, fund_id: int, from_date: str, to_date: str, search: str, asset_class: str, change_type: str) -> list[dict[str, Any]]:
        try:
            result = compare_snapshots(repo, fund_id, date.fromisoformat(from_date), date.fromisoformat(to_date))
        except ValueError as exc:
            raise _error(422, "invalid_date", "Comparison dates must use YYYY-MM-DD.") from exc
        rows = result.to_dicts()
        if search:
            needle = search.casefold()
            rows = [row for row in rows if needle in str(row.get("display_name_to") or row.get("display_name") or "").casefold()]
        if asset_class:
            rows = [row for row in rows if row.get("asset_class_effective") == asset_class]
        if change_type:
            rows = [row for row in rows if row.get("change_type") == change_type]
        return rows

    @app.get("/api/funds/{fund_id}/comparison")
    def comparison(fund_id: int, from_date: str, to_date: str, search: str = "", asset_class: str = "", change_type: str = "", page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=250)) -> dict[str, Any]:
        with repository() as repo:
            rows = comparison_data(repo, fund_id, from_date, to_date, search, asset_class, change_type)
        counts = {kind: sum(row.get("change_type") == kind for row in rows) for kind in ("introduced", "exited", "increased", "decreased", "unchanged")}
        start = (page - 1) * page_size
        return {"items": rows[start:start + page_size], "total": len(rows), "page": page, "page_size": page_size, "counts": counts}

    @app.get("/api/funds/{fund_id}/comparison.csv")
    def comparison_csv(fund_id: int, from_date: str, to_date: str, search: str = "", asset_class: str = "", change_type: str = "") -> Response:
        with repository() as repo:
            rows = comparison_data(repo, fund_id, from_date, to_date, search, asset_class, change_type)
        return _csv_response(rows, f"comparison-{from_date}-{to_date}.csv")

    async def process_import(file: UploadFile, validate_only: bool, replace: bool, amc: str, report_date: str | None, fund_code: str | None, fund_name: str | None, amc_name: str | None) -> dict[str, Any]:
        path = await _store_upload(file)
        original_name = file.filename or path.name
        try:
            metadata = _metadata(report_date, fund_code, fund_name, amc_name)
            if validate_only:
                result = ingest_file(path, None, dry_run=True, amc=amc, metadata=metadata)
            else:
                with repository() as repo:
                    result = ingest_file(path, repo, replace=replace, amc=amc, metadata=metadata)
            payload = result.to_dict()
            payload["path"] = original_name
            return payload
        except (OSError, ValueError) as exc:
            raise _error(400, "invalid_workbook", str(exc)) from exc
        finally:
            path.unlink(missing_ok=True)
            path.parent.rmdir()

    @app.post("/api/imports/validate")
    async def validate_import(file: UploadFile = File(...), amc: str = Form("auto"), report_date: str | None = Form(None), fund_code: str | None = Form(None), fund_name: str | None = Form(None), amc_name: str | None = Form(None)) -> dict[str, Any]:
        return await process_import(file, True, False, amc, report_date, fund_code, fund_name, amc_name)

    @app.post("/api/imports")
    async def commit_import(file: UploadFile = File(...), replace: bool = Form(False), amc: str = Form("auto"), report_date: str | None = Form(None), fund_code: str | None = Form(None), fund_name: str | None = Form(None), amc_name: str | None = Form(None)) -> dict[str, Any]:
        return await process_import(file, False, replace, amc, report_date, fund_code, fund_name, amc_name)

    @app.get("/api/imports")
    def imports(page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100)) -> dict[str, Any]:
        with repository() as repo:
            return repo.list_imports(page, page_size)

    @app.get("/api/imports/{source_file_id}")
    def import_detail(source_file_id: int) -> dict[str, Any]:
        with repository() as repo:
            result = repo.import_detail(source_file_id)
        if not result:
            raise _error(404, "import_not_found", "Import record was not found.")
        return result

    @app.post("/api/archive/verify")
    def verify() -> dict[str, list[str]]:
        with repository() as repo:
            return verify_archive(repo)

    @app.get("/api/bundles/export")
    def bundle_export() -> FileResponse:
        handle = tempfile.NamedTemporaryFile(prefix="mf-tracker-backup-", suffix=".zip", delete=False)
        handle.close()
        with repository() as repo:
            export_bundle(repo, handle.name)
        return FileResponse(
            handle.name,
            filename="mf-tracker-backup.zip",
            media_type="application/zip",
            background=BackgroundTask(Path(handle.name).unlink, missing_ok=True),
        )

    if FRONTEND_DIR.exists():
        app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
    return app


app = create_app(os.environ.get("MF_TRACKER_DB", "mf_tracker.sqlite3"), os.environ.get("MF_TRACKER_SOURCE_STORE"))
