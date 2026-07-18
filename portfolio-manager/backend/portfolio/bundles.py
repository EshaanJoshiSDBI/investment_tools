from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import zipfile
from typing import Any

from portfolio.errors import BundleError
from portfolio.models import Holding, RoundingMode, TargetWeight, WorkingStateRequest
from portfolio.persistence import SCHEMA_VERSION, SQLitePortfolioRepository
from portfolio.validation import validate_holdings, validate_target_weights


BUNDLE_VERSION = 1
FORMAT = "portfolio-manager-natural-keys-jsonl-v1"
TABLES = ("imports", "snapshots", "holdings", "settings", "targets", "prices")
MAX_BUNDLE_BYTES = 100 * 1024 * 1024


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _logical_rows(repository: SQLitePortfolioRepository) -> dict[str, list[dict[str, Any]]]:
    conn = repository.connection
    rows: dict[str, list[dict[str, Any]]] = {}
    rows["imports"] = [dict(row) for row in conn.execute(
        "SELECT ingestion_key,sha256,filename,file_size,parser_version,imported_at FROM portfolio_imports ORDER BY ingestion_key"
    )]
    rows["snapshots"] = [dict(row) for row in conn.execute(
        """SELECT s.snapshot_key,i.ingestion_key AS import_key,s.created_at,s.lifecycle_status,
                  s.superseded_at,next.snapshot_key AS superseded_by_snapshot_key,
                  restored.snapshot_key AS restored_from_snapshot_key
           FROM portfolio_snapshots s JOIN portfolio_imports i ON i.id=s.import_id
           LEFT JOIN portfolio_snapshots next ON next.id=s.superseded_by_snapshot_id
           LEFT JOIN portfolio_snapshots restored ON restored.id=s.restored_from_snapshot_id
           ORDER BY s.created_at,s.id"""
    )]
    for name, query in {
        "holdings": "SELECT s.snapshot_key,h.symbol,h.quantity,h.avg_price,h.uploaded_ltp FROM portfolio_holdings h JOIN portfolio_snapshots s ON s.id=h.snapshot_id ORDER BY s.snapshot_key,h.id",
        "settings": "SELECT s.snapshot_key,x.fresh_cash,x.rounding_mode,x.updated_at FROM portfolio_snapshot_settings x JOIN portfolio_snapshots s ON s.id=x.snapshot_id ORDER BY s.snapshot_key",
        "targets": "SELECT s.snapshot_key,t.symbol,t.target_weight_pct FROM portfolio_target_weights t JOIN portfolio_snapshots s ON s.id=t.snapshot_id ORDER BY s.snapshot_key,t.symbol",
        "prices": "SELECT s.snapshot_key,p.symbol,p.price,p.provider,p.observed_at FROM portfolio_price_observations p JOIN portfolio_snapshots s ON s.id=p.snapshot_id ORDER BY s.snapshot_key,p.observed_at,p.id",
    }.items():
        rows[name] = [dict(row) for row in conn.execute(query)]
    return rows


def export_bundle(repository: SQLitePortfolioRepository) -> bytes:
    logical = _logical_rows(repository)
    payloads = {
        table: "".join(_json(row) + "\n" for row in logical[table]).encode()
        for table in TABLES
    }
    manifest = {
        "bundle_version": BUNDLE_VERSION,
        "schema_version": SCHEMA_VERSION,
        "format": FORMAT,
        "tables": {
            table: {"rows": len(logical[table]), "sha256": hashlib.sha256(payload).hexdigest()}
            for table, payload in payloads.items()
        },
    }
    output = io.BytesIO()
    try:
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", json.dumps(manifest, indent=2, sort_keys=True))
            for table, payload in payloads.items():
                archive.writestr(f"tables/{table}.jsonl", payload)
    except (OSError, zipfile.BadZipFile) as exc:
        raise BundleError(f"Could not export portfolio bundle: {exc}") from exc
    return output.getvalue()


def _read_bundle(payload: bytes) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    if len(payload) > MAX_BUNDLE_BYTES:
        raise BundleError("Portfolio bundle cannot exceed 100 MiB")
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            names = set(archive.namelist())
            expected = {"manifest.json", *(f"tables/{table}.jsonl" for table in TABLES)}
            if names != expected:
                raise BundleError("Portfolio bundle contains missing or unexpected entries")
            manifest = json.loads(archive.read("manifest.json"))
            if manifest.get("bundle_version") != BUNDLE_VERSION or manifest.get("format") != FORMAT:
                raise BundleError("Unsupported portfolio bundle format or version")
            if manifest.get("schema_version", 0) > SCHEMA_VERSION:
                raise BundleError("Portfolio bundle schema is newer than this application")
            data: dict[str, list[dict[str, Any]]] = {}
            total_uncompressed = 0
            for table in TABLES:
                raw = archive.read(f"tables/{table}.jsonl")
                total_uncompressed += len(raw)
                if total_uncompressed > MAX_BUNDLE_BYTES:
                    raise BundleError("Portfolio bundle expands beyond 100 MiB")
                metadata = manifest.get("tables", {}).get(table, {})
                if hashlib.sha256(raw).hexdigest() != metadata.get("sha256"):
                    raise BundleError(f"Checksum mismatch for {table}")
                rows = [json.loads(line) for line in raw.decode().splitlines() if line]
                if len(rows) != metadata.get("rows"):
                    raise BundleError(f"Row count mismatch for {table}")
                data[table] = rows
            return manifest, data
    except BundleError:
        raise
    except (KeyError, UnicodeDecodeError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
        raise BundleError(f"Could not read portfolio bundle: {exc}") from exc


def import_bundle(payload: bytes, repository: SQLitePortfolioRepository) -> dict[str, Any]:
    if not repository.is_empty():
        raise BundleError("Portfolio bundle import requires an empty database")
    manifest, data = _read_bundle(payload)
    try:
        _validate_data(data)
        with repository.transaction() as conn:
            imports: dict[str, int] = {}
            for row in data["imports"]:
                conn.execute(
                    """INSERT INTO portfolio_imports(ingestion_key,sha256,filename,file_size,parser_version,imported_at)
                       VALUES(?,?,?,?,?,?)""",
                    tuple(row[key] for key in ("ingestion_key", "sha256", "filename", "file_size", "parser_version", "imported_at")),
                )
                imports[row["ingestion_key"]] = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            snapshots: dict[str, int] = {}
            for row in data["snapshots"]:
                conn.execute(
                    """INSERT INTO portfolio_snapshots(snapshot_key,import_id,created_at,lifecycle_status,superseded_at)
                       VALUES(?,?,?,?,?)""",
                    (row["snapshot_key"], imports[row["import_key"]], row["created_at"], row["lifecycle_status"], row["superseded_at"]),
                )
                snapshots[row["snapshot_key"]] = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            for row in data["snapshots"]:
                conn.execute(
                    "UPDATE portfolio_snapshots SET superseded_by_snapshot_id=?,restored_from_snapshot_id=? WHERE id=?",
                    (
                        snapshots.get(row["superseded_by_snapshot_key"]),
                        snapshots.get(row["restored_from_snapshot_key"]),
                        snapshots[row["snapshot_key"]],
                    ),
                )
            conn.executemany(
                "INSERT INTO portfolio_holdings(snapshot_id,symbol,quantity,avg_price,uploaded_ltp) VALUES(?,?,?,?,?)",
                [(snapshots[r["snapshot_key"]], r["symbol"], r["quantity"], r["avg_price"], r["uploaded_ltp"]) for r in data["holdings"]],
            )
            conn.executemany(
                "INSERT INTO portfolio_snapshot_settings(snapshot_id,fresh_cash,rounding_mode,updated_at) VALUES(?,?,?,?)",
                [(snapshots[r["snapshot_key"]], r["fresh_cash"], r["rounding_mode"], r["updated_at"]) for r in data["settings"]],
            )
            conn.executemany(
                "INSERT INTO portfolio_target_weights(snapshot_id,symbol,target_weight_pct) VALUES(?,?,?)",
                [(snapshots[r["snapshot_key"]], r["symbol"], r["target_weight_pct"]) for r in data["targets"]],
            )
            conn.executemany(
                "INSERT INTO portfolio_price_observations(snapshot_id,symbol,price,provider,observed_at) VALUES(?,?,?,?,?)",
                [(snapshots[r["snapshot_key"]], r["symbol"], r["price"], r["provider"], r["observed_at"]) for r in data["prices"]],
            )
            if conn.execute("PRAGMA foreign_key_check").fetchall():
                raise BundleError("Portfolio bundle import produced foreign-key violations")
        return manifest
    except BundleError:
        raise
    except (KeyError, TypeError, ValueError, sqlite3.Error) as exc:
        raise BundleError(f"Could not import portfolio bundle: {exc}") from exc


def _validate_data(data: dict[str, list[dict[str, Any]]]) -> None:
    import_keys = [row["ingestion_key"] for row in data["imports"]]
    snapshot_keys = [row["snapshot_key"] for row in data["snapshots"]]
    if len(import_keys) != len(set(import_keys)) or len(snapshot_keys) != len(set(snapshot_keys)):
        raise BundleError("Portfolio bundle contains duplicate natural keys")
    active_count = sum(row["lifecycle_status"] == "active" for row in data["snapshots"])
    if data["snapshots"] and active_count != 1:
        raise BundleError("Portfolio bundle must contain exactly one active snapshot")
    imports = set(import_keys)
    snapshots = set(snapshot_keys)
    for row in data["snapshots"]:
        if row["import_key"] not in imports:
            raise BundleError("Portfolio bundle references an unknown import")
        for field in ("superseded_by_snapshot_key", "restored_from_snapshot_key"):
            if row[field] is not None and row[field] not in snapshots:
                raise BundleError("Portfolio bundle references an unknown snapshot")
        if row["lifecycle_status"] == "active" and (
            row["superseded_at"] is not None or row["superseded_by_snapshot_key"] is not None
        ):
            raise BundleError("Active portfolio snapshot has invalid lifecycle metadata")
        if row["lifecycle_status"] == "superseded" and (
            row["superseded_at"] is None or row["superseded_by_snapshot_key"] is None
        ):
            raise BundleError("Superseded portfolio snapshot has incomplete lifecycle metadata")
    grouped_holdings: dict[str, list[Holding]] = {key: [] for key in snapshots}
    grouped_targets: dict[str, list[TargetWeight]] = {key: [] for key in snapshots}
    for row in data["holdings"]:
        grouped_holdings[row["snapshot_key"]].append(Holding(
            symbol=row["symbol"], quantity=row["quantity"], avg_price=row["avg_price"], ltp=row["uploaded_ltp"]
        ))
    for key, holdings in grouped_holdings.items():
        validate_holdings(holdings)
    for row in data["targets"]:
        grouped_targets[row["snapshot_key"]].append(TargetWeight(
            symbol=row["symbol"], target_weight_pct=row["target_weight_pct"]
        ))
    for key, targets in grouped_targets.items():
        validate_target_weights(targets, grouped_holdings[key])
    if {row["snapshot_key"] for row in data["settings"]} != snapshots:
        raise BundleError("Portfolio bundle must contain one settings row per snapshot")
    for row in data["settings"]:
        WorkingStateRequest(
            target_weights=grouped_targets[row["snapshot_key"]], fresh_cash=row["fresh_cash"],
            rounding_mode=RoundingMode(row["rounding_mode"]),
        )
    for row in data["prices"]:
        snapshot_key = row["snapshot_key"]
        holding_symbols = {holding.symbol for holding in grouped_holdings.get(snapshot_key, [])}
        if (
            snapshot_key not in snapshots
            or row["symbol"] not in holding_symbols
            or float(row["price"]) <= 0
        ):
            raise BundleError("Portfolio bundle contains an invalid price observation")
