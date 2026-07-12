from __future__ import annotations

import hashlib
import json
import sqlite3
import zipfile
from pathlib import Path
from typing import Any

from .errors import BundleError
from .persistence import SCHEMA_VERSION, SQLiteRepository

BUNDLE_VERSION = 1
TABLES = ("amcs", "funds", "source_files", "instruments", "snapshots", "holdings", "ingestion_issues")


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _snapshot_key(ingestion_key: str, amc_slug: str, fund_code: str, report_date: str) -> str:
    return "|".join((ingestion_key, amc_slug, fund_code, report_date))


def _logical_rows(repository: SQLiteRepository) -> dict[str, list[dict[str, Any]]]:
    conn = repository.connection
    rows: dict[str, list[dict[str, Any]]] = {}
    rows["amcs"] = [dict(row) for row in conn.execute("SELECT slug,name FROM amcs ORDER BY slug")]
    rows["funds"] = [dict(row) for row in conn.execute(
        "SELECT a.slug AS amc_slug,f.sheet_code AS fund_code,f.name,f.status FROM funds f JOIN amcs a ON a.id=f.amc_id ORDER BY a.slug,f.sheet_code"
    )]
    source_columns = "ingestion_key,sha256,filename,file_size,report_date,parser_version,reader,status,metadata_overrides_json,effective_metadata_json,archive_relpath,ingested_at"
    rows["source_files"] = [dict(row) for row in conn.execute(f"SELECT {source_columns} FROM source_files ORDER BY ingestion_key")]
    rows["instruments"] = [dict(row) for row in conn.execute(
        "SELECT a.slug AS amc_slug,i.identity_key,i.isin,i.normalized_name,i.display_name,i.asset_class,i.instrument_type FROM instruments i JOIN amcs a ON a.id=i.amc_id ORDER BY a.slug,i.identity_key"
    )]
    snapshot_rows = conn.execute("""SELECT s.*,a.slug AS amc_slug,f.sheet_code AS fund_code,sf.ingestion_key,
        successor_source.ingestion_key AS successor_ingestion_key,successor_amc.slug AS successor_amc_slug,
        successor_fund.sheet_code AS successor_fund_code,successor.report_date AS successor_report_date
        FROM snapshots s JOIN funds f ON f.id=s.fund_id JOIN amcs a ON a.id=f.amc_id
        JOIN source_files sf ON sf.id=s.source_file_id
        LEFT JOIN snapshots successor ON successor.id=s.superseded_by_snapshot_id
        LEFT JOIN source_files successor_source ON successor_source.id=successor.source_file_id
        LEFT JOIN funds successor_fund ON successor_fund.id=successor.fund_id
        LEFT JOIN amcs successor_amc ON successor_amc.id=successor_fund.amc_id
        ORDER BY sf.ingestion_key,a.slug,f.sheet_code,s.report_date""").fetchall()
    rows["snapshots"] = []
    id_to_key: dict[int, str] = {}
    for row in snapshot_rows:
        key = _snapshot_key(row["ingestion_key"], row["amc_slug"], row["fund_code"], row["report_date"])
        id_to_key[row["id"]] = key
        successor_key = None
        if row["successor_ingestion_key"]:
            successor_key = _snapshot_key(row["successor_ingestion_key"], row["successor_amc_slug"], row["successor_fund_code"], row["successor_report_date"])
        rows["snapshots"].append({
            "snapshot_key": key, "amc_slug": row["amc_slug"], "fund_code": row["fund_code"],
            "source_ingestion_key": row["ingestion_key"], "report_date": row["report_date"],
            "lifecycle_status": row["lifecycle_status"], "superseded_at": row["superseded_at"],
            "superseded_by_snapshot_key": successor_key,
            "reported_total_value_lakh": row["reported_total_value_lakh"],
            "reported_total_weight": row["reported_total_weight"],
        })
    holding_columns = [row[1] for row in conn.execute("PRAGMA table_info(holdings)") if row[1] not in {"id", "snapshot_id", "instrument_id"}]
    rows["holdings"] = []
    for row in conn.execute("SELECT * FROM holdings ORDER BY snapshot_id,id"):
        item = {column: row[column] for column in holding_columns}
        item["snapshot_key"] = id_to_key[row["snapshot_id"]]
        rows["holdings"].append(item)
    rows["ingestion_issues"] = []
    for row in conn.execute("SELECT ii.*,sf.ingestion_key FROM ingestion_issues ii JOIN source_files sf ON sf.id=ii.source_file_id ORDER BY ii.id"):
        rows["ingestion_issues"].append({
            "source_ingestion_key": row["ingestion_key"],
            "snapshot_key": id_to_key.get(row["snapshot_id"]),
            **{column: row[column] for column in ("severity", "code", "message", "sheet", "source_row", "raw_value")},
        })
    return rows


def export_bundle(repository: SQLiteRepository, output: str | Path) -> dict[str, Any]:
    destination = Path(output)
    if destination.exists():
        raise BundleError(f"bundle already exists: {destination}")
    manifest = {"bundle_version": BUNDLE_VERSION, "schema_version": SCHEMA_VERSION, "format": "mf-tracker-natural-keys-jsonl-v1", "tables": list(TABLES)}
    try:
        logical = _logical_rows(repository)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", json.dumps(manifest, indent=2, sort_keys=True))
            for table in TABLES:
                archive.writestr(f"tables/{table}.jsonl", "".join(_json(row) + "\n" for row in logical[table]))
            if repository.source_archive:
                objects: set[str] = set()
                for row in repository.connection.execute("SELECT sha256,archive_relpath FROM source_files WHERE archive_relpath IS NOT NULL"):
                    if row["sha256"] in objects:
                        continue
                    source = repository.source_archive.root / row["archive_relpath"]
                    if not source.is_file() or _hash(source.read_bytes()) != row["sha256"]:
                        raise BundleError(f"missing or corrupt archived source: {row['archive_relpath']}")
                    archive.write(source, f"sources/{row['sha256']}.bin")
                    objects.add(row["sha256"])
        return manifest
    except BundleError:
        destination.unlink(missing_ok=True)
        raise
    except (OSError, sqlite3.Error, zipfile.BadZipFile) as exc:
        destination.unlink(missing_ok=True)
        raise BundleError(f"could not export bundle: {exc}") from exc


def _read_bundle(bundle: str | Path) -> tuple[zipfile.ZipFile, dict[str, Any], dict[str, list[dict[str, Any]]]]:
    archive = zipfile.ZipFile(bundle)
    manifest = json.loads(archive.read("manifest.json"))
    if manifest.get("bundle_version") != BUNDLE_VERSION or manifest.get("format") != "mf-tracker-natural-keys-jsonl-v1":
        archive.close()
        raise BundleError("unsupported bundle format or version")
    if manifest.get("schema_version", 0) > SCHEMA_VERSION:
        archive.close()
        raise BundleError("bundle schema is newer than this application")
    data = {table: [json.loads(line) for line in archive.read(f"tables/{table}.jsonl").decode().splitlines() if line] for table in TABLES}
    return archive, manifest, data


def import_bundle(bundle: str | Path, repository: SQLiteRepository) -> dict[str, Any]:
    if repository.connection.execute("SELECT 1 FROM source_files LIMIT 1").fetchone():
        raise BundleError("destination database is not empty")
    archive: zipfile.ZipFile | None = None
    created_targets: list[Path] = []
    try:
        archive, manifest, data = _read_bundle(bundle)
        if any(row.get("archive_relpath") for row in data["source_files"]) and not repository.source_archive:
            raise BundleError("bundle contains source files but destination has no source archive")
        if repository.source_archive:
            for row in data["source_files"]:
                if not row.get("archive_relpath"):
                    continue
                payload = archive.read(f"sources/{row['sha256']}.bin")
                if _hash(payload) != row["sha256"] or len(payload) != row["file_size"]:
                    raise BundleError(f"source hash/size mismatch for {row['filename']}")
                target = repository.source_archive.root / row["archive_relpath"]
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists() and _hash(target.read_bytes()) != row["sha256"]:
                    raise BundleError(f"existing archive object is corrupt: {row['archive_relpath']}")
                if not target.exists():
                    temporary = target.with_name(target.name + ".importing")
                    temporary.write_bytes(payload)
                    temporary.replace(target)
                    created_targets.append(target)
        with repository.transaction() as conn:
            amcs: dict[str, int] = {}
            for row in data["amcs"]:
                conn.execute("INSERT INTO amcs(slug,name) VALUES(?,?)", (row["slug"], row["name"]))
                amcs[row["slug"]] = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            funds: dict[tuple[str, str], int] = {}
            for row in data["funds"]:
                conn.execute("INSERT INTO funds(amc_id,sheet_code,name,status) VALUES(?,?,?,?)", (amcs[row["amc_slug"]], row["fund_code"], row["name"], row["status"]))
                funds[(row["amc_slug"], row["fund_code"])] = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            sources: dict[str, int] = {}
            source_columns = list(data["source_files"][0]) if data["source_files"] else []
            for row in data["source_files"]:
                conn.execute(f"INSERT INTO source_files({','.join(source_columns)}) VALUES({','.join('?' for _ in source_columns)})", tuple(row[column] for column in source_columns))
                sources[row["ingestion_key"]] = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            instruments: dict[tuple[str, str], int] = {}
            for row in data["instruments"]:
                columns = ("identity_key", "isin", "normalized_name", "display_name", "asset_class", "instrument_type")
                conn.execute(f"INSERT INTO instruments(amc_id,{','.join(columns)}) VALUES(?,{','.join('?' for _ in columns)})", (amcs[row["amc_slug"]], *(row[column] for column in columns)))
                instruments[(row["amc_slug"], row["identity_key"])] = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            snapshots: dict[str, int] = {}
            for row in data["snapshots"]:
                conn.execute("""INSERT INTO snapshots(fund_id,source_file_id,report_date,lifecycle_status,superseded_at,reported_total_value_lakh,reported_total_weight)
                    VALUES(?,?,?,?,?,?,?)""", (funds[(row["amc_slug"], row["fund_code"])], sources[row["source_ingestion_key"]], row["report_date"], row["lifecycle_status"], row["superseded_at"], row["reported_total_value_lakh"], row["reported_total_weight"]))
                snapshots[row["snapshot_key"]] = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            for row in data["snapshots"]:
                if row["superseded_by_snapshot_key"]:
                    conn.execute("UPDATE snapshots SET superseded_by_snapshot_id=? WHERE id=?", (snapshots[row["superseded_by_snapshot_key"]], snapshots[row["snapshot_key"]]))
            for row in data["holdings"]:
                snapshot = next(item for item in data["snapshots"] if item["snapshot_key"] == row["snapshot_key"])
                values = {key: value for key, value in row.items() if key != "snapshot_key"}
                columns = list(values)
                conn.execute(f"INSERT INTO holdings(snapshot_id,instrument_id,{','.join(columns)}) VALUES(?,?,{','.join('?' for _ in columns)})", (snapshots[row["snapshot_key"]], instruments[(snapshot["amc_slug"], row["identity_key"])], *(values[column] for column in columns)))
            for row in data["ingestion_issues"]:
                columns = ("severity", "code", "message", "sheet", "source_row", "raw_value")
                conn.execute(f"INSERT INTO ingestion_issues(source_file_id,snapshot_id,{','.join(columns)}) VALUES(?,?,{','.join('?' for _ in columns)})", (sources[row["source_ingestion_key"]], snapshots.get(row["snapshot_key"]), *(row[column] for column in columns)))
            if conn.execute("PRAGMA foreign_key_check").fetchall():
                raise BundleError("bundle import produced foreign-key violations")
        return manifest
    except BundleError:
        for target in created_targets:
            target.unlink(missing_ok=True)
        raise
    except (KeyError, StopIteration, ValueError, OSError, sqlite3.Error, zipfile.BadZipFile) as exc:
        for target in created_targets:
            target.unlink(missing_ok=True)
        raise BundleError(f"could not import bundle: {exc}") from exc
    finally:
        if archive:
            archive.close()


def verify_archive(repository: SQLiteRepository) -> dict[str, list[str]]:
    return repository.archive_report()


def _hash(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()
