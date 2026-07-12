from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Protocol

from .domain import ParsedWorkbook
from .errors import MigrationError, PersistenceError, SnapshotConflictError, SourceArchiveError

SCHEMA_VERSION = 3

LEGACY_SCHEMA = """
CREATE TABLE IF NOT EXISTS amcs (id INTEGER PRIMARY KEY, slug TEXT NOT NULL UNIQUE, name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS funds (
 id INTEGER PRIMARY KEY, amc_id INTEGER NOT NULL REFERENCES amcs(id), sheet_code TEXT NOT NULL,
 name TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active', UNIQUE(amc_id, sheet_code));
CREATE TABLE IF NOT EXISTS source_files (
 id INTEGER PRIMARY KEY, sha256 TEXT NOT NULL UNIQUE, filename TEXT NOT NULL, file_size INTEGER NOT NULL,
 report_date TEXT NOT NULL, parser_version TEXT NOT NULL, reader TEXT NOT NULL, status TEXT NOT NULL,
 metadata_overrides_json TEXT NOT NULL DEFAULT '{}', ingested_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS snapshots (
 id INTEGER PRIMARY KEY, fund_id INTEGER NOT NULL REFERENCES funds(id), source_file_id INTEGER NOT NULL REFERENCES source_files(id),
 report_date TEXT NOT NULL, reported_total_value_lakh REAL, reported_total_weight REAL, UNIQUE(fund_id, report_date));
CREATE TABLE IF NOT EXISTS instruments (
 id INTEGER PRIMARY KEY, amc_id INTEGER NOT NULL REFERENCES amcs(id), identity_key TEXT NOT NULL, isin TEXT,
 normalized_name TEXT NOT NULL, display_name TEXT NOT NULL, asset_class TEXT NOT NULL, instrument_type TEXT NOT NULL,
 UNIQUE(amc_id, identity_key));
CREATE TABLE IF NOT EXISTS holdings (
 id INTEGER PRIMARY KEY, snapshot_id INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
 instrument_id INTEGER NOT NULL REFERENCES instruments(id), source_sheet TEXT NOT NULL, source_row INTEGER NOT NULL,
 source_instrument_code TEXT, source_name TEXT NOT NULL, section TEXT, subsection TEXT, industry_rating TEXT,
 quantity REAL, market_value_lakh REAL NOT NULL, weight REAL NOT NULL, ytm REAL, ytc REAL, direction TEXT, expiry TEXT,
 UNIQUE(snapshot_id, instrument_id, source_row));
CREATE TABLE IF NOT EXISTS ingestion_issues (
 id INTEGER PRIMARY KEY, source_file_id INTEGER NOT NULL REFERENCES source_files(id),
 snapshot_id INTEGER REFERENCES snapshots(id) ON DELETE CASCADE, severity TEXT NOT NULL, code TEXT NOT NULL,
 message TEXT NOT NULL, sheet TEXT, source_row INTEGER, raw_value TEXT);
"""

LATEST_SCHEMA = """
CREATE TABLE IF NOT EXISTS amcs (id INTEGER PRIMARY KEY, slug TEXT NOT NULL UNIQUE, name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS funds (
 id INTEGER PRIMARY KEY, amc_id INTEGER NOT NULL REFERENCES amcs(id), sheet_code TEXT NOT NULL,
 name TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active', UNIQUE(amc_id, sheet_code));
CREATE TABLE IF NOT EXISTS source_files (
 id INTEGER PRIMARY KEY, ingestion_key TEXT NOT NULL UNIQUE, sha256 TEXT NOT NULL, filename TEXT NOT NULL,
 file_size INTEGER NOT NULL, report_date TEXT NOT NULL, parser_version TEXT NOT NULL, reader TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'ingested', metadata_overrides_json TEXT NOT NULL DEFAULT '{}',
 effective_metadata_json TEXT NOT NULL DEFAULT '{}', archive_relpath TEXT, ingested_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS snapshots (
 id INTEGER PRIMARY KEY, fund_id INTEGER NOT NULL REFERENCES funds(id), source_file_id INTEGER NOT NULL REFERENCES source_files(id),
 report_date TEXT NOT NULL, lifecycle_status TEXT NOT NULL DEFAULT 'active' CHECK(lifecycle_status IN ('active','superseded')),
 superseded_at TEXT, superseded_by_snapshot_id INTEGER REFERENCES snapshots(id),
 reported_total_value_lakh REAL, reported_total_weight REAL);
CREATE TABLE IF NOT EXISTS instruments (
 id INTEGER PRIMARY KEY, amc_id INTEGER NOT NULL REFERENCES amcs(id), identity_key TEXT NOT NULL, isin TEXT,
 normalized_name TEXT NOT NULL, display_name TEXT NOT NULL, asset_class TEXT NOT NULL, instrument_type TEXT NOT NULL,
 UNIQUE(amc_id, identity_key));
CREATE TABLE IF NOT EXISTS holdings (
 id INTEGER PRIMARY KEY, snapshot_id INTEGER NOT NULL REFERENCES snapshots(id), instrument_id INTEGER NOT NULL REFERENCES instruments(id),
 identity_key TEXT NOT NULL, isin TEXT, normalized_name TEXT NOT NULL, display_name TEXT NOT NULL,
 asset_class TEXT NOT NULL, instrument_type TEXT NOT NULL, source_sheet TEXT NOT NULL, source_row INTEGER NOT NULL,
 source_instrument_code TEXT, source_name TEXT NOT NULL, section TEXT, subsection TEXT, industry_rating TEXT,
 quantity REAL, market_value_lakh REAL NOT NULL, weight REAL NOT NULL, ytm REAL, ytc REAL, direction TEXT, expiry TEXT,
 UNIQUE(snapshot_id, instrument_id, source_row));
CREATE TABLE IF NOT EXISTS ingestion_issues (
 id INTEGER PRIMARY KEY, source_file_id INTEGER NOT NULL REFERENCES source_files(id),
 snapshot_id INTEGER REFERENCES snapshots(id), severity TEXT NOT NULL, code TEXT NOT NULL,
 message TEXT NOT NULL, sheet TEXT, source_row INTEGER, raw_value TEXT);
CREATE TABLE IF NOT EXISTS schema_migrations (
 version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE UNIQUE INDEX IF NOT EXISTS uq_active_snapshot ON snapshots(fund_id, report_date) WHERE lifecycle_status='active';
CREATE INDEX IF NOT EXISTS idx_snapshots_fund_date ON snapshots(fund_id, report_date);
CREATE INDEX IF NOT EXISTS idx_instruments_isin ON instruments(isin);
CREATE INDEX IF NOT EXISTS idx_instruments_asset ON instruments(asset_class);
CREATE INDEX IF NOT EXISTS idx_holdings_instrument ON holdings(instrument_id, snapshot_id);
CREATE INDEX IF NOT EXISTS idx_source_hash ON source_files(sha256);
CREATE VIEW IF NOT EXISTS source_file_states AS
SELECT sf.id AS source_file_id,
 CASE
  WHEN COUNT(s.id)=0 THEN 'no_snapshots'
  WHEN SUM(CASE WHEN s.lifecycle_status='active' THEN 1 ELSE 0 END)=COUNT(s.id) THEN 'active'
  WHEN SUM(CASE WHEN s.lifecycle_status='active' THEN 1 ELSE 0 END)=0 THEN 'superseded'
  ELSE 'partially_superseded'
 END AS lifecycle_status,
 COUNT(s.id) AS snapshot_count,
 SUM(CASE WHEN s.lifecycle_status='active' THEN 1 ELSE 0 END) AS active_snapshot_count
FROM source_files sf LEFT JOIN snapshots s ON s.source_file_id=sf.id GROUP BY sf.id;
"""


def _execute_script(connection: sqlite3.Connection, script: str) -> None:
    for statement in script.split(";"):
        if statement.strip():
            connection.execute(statement)


def effective_metadata(parsed: ParsedWorkbook) -> dict[str, Any]:
    return {
        "amc_slug": parsed.amc_slug,
        "amc_name": parsed.amc_name,
        "report_date": parsed.report_date.isoformat(),
        "funds": [{"fund_code": s.sheet_code, "fund_name": s.fund_name} for s in parsed.snapshots],
        "overrides": parsed.metadata_overrides,
    }


def ingestion_key(parsed: ParsedWorkbook) -> str:
    identity = {
        "sha256": parsed.sha256,
        "parser_version": parsed.parser_version,
        "metadata": effective_metadata(parsed),
    }
    payload = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class SaveOutcome:
    source_file_id: int | None
    status: str
    effective_metadata: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ArchiveEntry:
    relative_path: str
    created: bool


class SourceArchive:
    def __init__(self, root: str | Path):
        self.root = Path(root)

    def store(self, source: Path, expected_hash: str, expected_size: int) -> ArchiveEntry:
        destination = self.root / expected_hash[:2] / expected_hash / "source.bin"
        try:
            if destination.exists():
                if destination.stat().st_size != expected_size or _file_hash(destination) != expected_hash:
                    raise SourceArchiveError(f"corrupt archived source for {expected_hash}")
                return ArchiveEntry(destination.relative_to(self.root).as_posix(), False)
            destination.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=".source-", dir=destination.parent)
            os.close(fd)
            temp_path = Path(temporary)
            try:
                shutil.copyfile(source, temp_path)
                if temp_path.stat().st_size != expected_size or _file_hash(temp_path) != expected_hash:
                    raise SourceArchiveError(f"source changed while archiving {source}")
                os.replace(temp_path, destination)
            finally:
                temp_path.unlink(missing_ok=True)
            return ArchiveEntry(destination.relative_to(self.root).as_posix(), True)
        except SourceArchiveError:
            raise
        except OSError as exc:
            raise SourceArchiveError(f"could not archive {source}: {exc}") from exc

    def discard(self, relative_path: str) -> None:
        path = self.root / relative_path
        path.unlink(missing_ok=True)
        for parent in (path.parent, path.parent.parent):
            try:
                parent.rmdir()
            except OSError:
                break

    def verify(self, references: dict[str, str]) -> dict[str, list[str]]:
        missing: list[str] = []
        corrupt: list[str] = []
        expected_paths = set(references.values())
        for sha256, relative in references.items():
            path = self.root / relative
            if not path.is_file():
                missing.append(relative)
            elif _file_hash(path) != sha256:
                corrupt.append(relative)
        present = {
            path.relative_to(self.root).as_posix()
            for path in self.root.glob("*/*/source.bin") if path.is_file()
        } if self.root.exists() else set()
        return {"missing": sorted(missing), "corrupt": sorted(corrupt), "unreferenced": sorted(present - expected_paths)}


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class Repository(Protocol):
    def save_workbook(self, parsed: ParsedWorkbook, *, replace: bool = False) -> SaveOutcome: ...


class SQLiteRepository:
    def __init__(self, path: str | Path, *, source_archive: SourceArchive | None = None, busy_timeout_ms: int = 5000):
        self.path = str(path)
        self.source_archive = source_archive
        try:
            self.connection = sqlite3.connect(self.path, timeout=busy_timeout_ms / 1000)
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA foreign_keys = ON")
            self.connection.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
            self._migrate()
        except MigrationError:
            if hasattr(self, "connection"):
                self.connection.close()
            raise
        except (sqlite3.Error, OSError) as exc:
            if hasattr(self, "connection"):
                self.connection.close()
            raise PersistenceError(f"could not open database {self.path}: {exc}") from exc

    def _migrate(self) -> None:
        version = self.connection.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise MigrationError(f"database schema {version} is newer than supported schema {SCHEMA_VERSION}")
        has_tables = self.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' LIMIT 1"
        ).fetchone()
        if not has_tables:
            with self.connection:
                _execute_script(self.connection, LATEST_SCHEMA)
                self.connection.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES(?)", (SCHEMA_VERSION,))
                self.connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            return
        if version == 0:
            raise MigrationError("existing unversioned database cannot be migrated safely")
        if version == 1:
            columns = {row[1] for row in self.connection.execute("PRAGMA table_info(source_files)")}
            with self.connection:
                _execute_script(self.connection, LEGACY_SCHEMA)
                if "metadata_overrides_json" not in columns:
                    self.connection.execute("ALTER TABLE source_files ADD COLUMN metadata_overrides_json TEXT NOT NULL DEFAULT '{}'")
                self.connection.execute("PRAGMA user_version = 2")
            version = 2
        if version == 2:
            self._migrate_v2_to_v3()
        _execute_script(self.connection, LATEST_SCHEMA)
        self.connection.commit()

    def _migrate_v2_to_v3(self) -> None:
        conn = self.connection
        try:
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute("BEGIN IMMEDIATE")
            for table in ("source_files", "snapshots", "holdings", "ingestion_issues"):
                exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
                if exists:
                    conn.execute(f"ALTER TABLE {table} RENAME TO {table}_v2")
            _execute_script(conn, LATEST_SCHEMA)
            rows = conn.execute("SELECT * FROM source_files_v2").fetchall()
            for row in rows:
                metadata = json.loads(row["metadata_overrides_json"] or "{}")
                snapshot_metadata = conn.execute(
                    """SELECT a.slug AS amc_slug,a.name AS amc_name,f.sheet_code AS fund_code,f.name AS fund_name
                    FROM snapshots_v2 s JOIN funds f ON f.id=s.fund_id JOIN amcs a ON a.id=f.amc_id
                    WHERE s.source_file_id=? ORDER BY s.id""",
                    (row["id"],),
                ).fetchall()
                legacy_effective = {
                    "report_date": row["report_date"],
                    "overrides": metadata,
                    "funds": [
                        {"fund_code": item["fund_code"], "fund_name": item["fund_name"]}
                        for item in snapshot_metadata
                    ],
                }
                if snapshot_metadata:
                    legacy_effective.update(
                        amc_slug=snapshot_metadata[0]["amc_slug"],
                        amc_name=snapshot_metadata[0]["amc_name"],
                    )
                key_payload = json.dumps({"sha256": row["sha256"], "parser_version": row["parser_version"], "metadata": legacy_effective}, sort_keys=True, separators=(",", ":"))
                conn.execute(
                    "INSERT INTO source_files(id,ingestion_key,sha256,filename,file_size,report_date,parser_version,reader,status,metadata_overrides_json,effective_metadata_json,ingested_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (row["id"], hashlib.sha256(key_payload.encode()).hexdigest(), row["sha256"], row["filename"], row["file_size"], row["report_date"], row["parser_version"], row["reader"], row["status"], row["metadata_overrides_json"], json.dumps(legacy_effective, sort_keys=True), row["ingested_at"]),
                )
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='snapshots_v2'").fetchone():
                conn.execute("INSERT INTO snapshots(id,fund_id,source_file_id,report_date,reported_total_value_lakh,reported_total_weight) SELECT id,fund_id,source_file_id,report_date,reported_total_value_lakh,reported_total_weight FROM snapshots_v2")
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='holdings_v2'").fetchone():
                conn.execute("""INSERT INTO holdings(id,snapshot_id,instrument_id,identity_key,isin,normalized_name,display_name,asset_class,instrument_type,source_sheet,source_row,source_instrument_code,source_name,section,subsection,industry_rating,quantity,market_value_lakh,weight,ytm,ytc,direction,expiry)
                    SELECT h.id,h.snapshot_id,h.instrument_id,i.identity_key,i.isin,i.normalized_name,i.display_name,i.asset_class,i.instrument_type,h.source_sheet,h.source_row,h.source_instrument_code,h.source_name,h.section,h.subsection,h.industry_rating,h.quantity,h.market_value_lakh,h.weight,h.ytm,h.ytc,h.direction,h.expiry FROM holdings_v2 h JOIN instruments i ON i.id=h.instrument_id""")
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ingestion_issues_v2'").fetchone():
                conn.execute("INSERT INTO ingestion_issues SELECT * FROM ingestion_issues_v2")
            for table in ("ingestion_issues_v2", "holdings_v2", "snapshots_v2", "source_files_v2"):
                conn.execute(f"DROP TABLE IF EXISTS {table}")
            conn.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES(3)")
            conn.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES(2)")
            conn.execute("PRAGMA user_version = 3")
            violations = conn.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise MigrationError(f"foreign-key violations after migration: {len(violations)}")
            conn.commit()
        except Exception as exc:
            conn.rollback()
            if isinstance(exc, MigrationError):
                raise
            raise MigrationError(f"could not migrate schema 2 to 3: {exc}") from exc
        finally:
            conn.execute("PRAGMA foreign_keys = ON")

    def close(self) -> None:
        self.connection.close()

    def has_source(self, sha256: str) -> bool:
        """Compatibility helper; ingestion idempotency itself uses ingestion_key."""
        return self.connection.execute(
            "SELECT 1 FROM source_files WHERE sha256=?", (sha256,)
        ).fetchone() is not None

    def __enter__(self) -> "SQLiteRepository":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            yield self.connection
        except Exception:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def save_workbook(self, parsed: ParsedWorkbook, *, replace: bool = False) -> SaveOutcome:
        key = ingestion_key(parsed)
        metadata = effective_metadata(parsed)
        archive_entry: ArchiveEntry | None = None
        try:
            archive_entry = self.source_archive.store(parsed.path, parsed.sha256, parsed.file_size) if self.source_archive else None
            archive_relpath = archive_entry.relative_path if archive_entry else None
            with self.transaction() as conn:
                duplicate = conn.execute("SELECT id,effective_metadata_json FROM source_files WHERE ingestion_key=?", (key,)).fetchone()
                if duplicate:
                    if archive_relpath:
                        conn.execute("UPDATE source_files SET archive_relpath=COALESCE(archive_relpath,?) WHERE id=?", (archive_relpath, duplicate["id"]))
                    return SaveOutcome(duplicate["id"], "no_op", json.loads(duplicate["effective_metadata_json"]))
                conn.execute("INSERT INTO amcs(slug,name) VALUES(?,?) ON CONFLICT(slug) DO UPDATE SET name=excluded.name", (parsed.amc_slug, parsed.amc_name))
                amc_id = conn.execute("SELECT id FROM amcs WHERE slug=?", (parsed.amc_slug,)).fetchone()[0]
                conflicts: dict[tuple[int, str], int] = {}
                fund_ids: dict[str, int] = {}
                for snapshot in parsed.snapshots:
                    conn.execute("INSERT INTO funds(amc_id,sheet_code,name) VALUES(?,?,?) ON CONFLICT(amc_id,sheet_code) DO UPDATE SET name=excluded.name", (amc_id, snapshot.sheet_code, snapshot.fund_name))
                    fund_id = conn.execute("SELECT id FROM funds WHERE amc_id=? AND sheet_code=?", (amc_id, snapshot.sheet_code)).fetchone()[0]
                    fund_ids[snapshot.sheet_code] = fund_id
                    existing = conn.execute("SELECT id FROM snapshots WHERE fund_id=? AND report_date=? AND lifecycle_status='active'", (fund_id, snapshot.report_date.isoformat())).fetchone()
                    if existing:
                        if not replace:
                            raise SnapshotConflictError(f"Snapshot already exists for {snapshot.sheet_code} on {snapshot.report_date}")
                        conflicts[(fund_id, snapshot.report_date.isoformat())] = existing["id"]
                conn.execute("""INSERT INTO source_files(ingestion_key,sha256,filename,file_size,report_date,parser_version,reader,status,metadata_overrides_json,effective_metadata_json,archive_relpath)
                    VALUES(?,?,?,?,?,?,?,'ingested',?,?,?)""", (key, parsed.sha256, parsed.path.name, parsed.file_size, parsed.report_date.isoformat(), parsed.parser_version, parsed.reader, json.dumps(parsed.metadata_overrides, sort_keys=True), json.dumps(metadata, sort_keys=True), archive_relpath))
                source_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                for issue in parsed.issues:
                    conn.execute("INSERT INTO ingestion_issues(source_file_id,severity,code,message,sheet,source_row,raw_value) VALUES(?,?,?,?,?,?,?)", (source_id, issue.severity, issue.code, issue.message, issue.sheet, issue.row, issue.raw_value))
                for old_id in conflicts.values():
                    conn.execute("UPDATE snapshots SET lifecycle_status='superseded',superseded_at=CURRENT_TIMESTAMP WHERE id=?", (old_id,))
                for snapshot in parsed.snapshots:
                    fund_id = fund_ids[snapshot.sheet_code]
                    conn.execute("INSERT INTO snapshots(fund_id,source_file_id,report_date,reported_total_value_lakh,reported_total_weight) VALUES(?,?,?,?,?)", (fund_id, source_id, snapshot.report_date.isoformat(), snapshot.reported_total_value_lakh, snapshot.reported_total_weight))
                    snapshot_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                    for holding in snapshot.holdings:
                        conn.execute("""INSERT INTO instruments(amc_id,identity_key,isin,normalized_name,display_name,asset_class,instrument_type) VALUES(?,?,?,?,?,?,?)
                            ON CONFLICT(amc_id,identity_key) DO UPDATE SET isin=COALESCE(excluded.isin,instruments.isin),normalized_name=excluded.normalized_name,display_name=excluded.display_name,asset_class=excluded.asset_class,instrument_type=excluded.instrument_type""", (amc_id, holding.identity_key, holding.isin, holding.normalized_name, holding.source_name, holding.asset_class, holding.instrument_type))
                        instrument_id = conn.execute("SELECT id FROM instruments WHERE amc_id=? AND identity_key=?", (amc_id, holding.identity_key)).fetchone()[0]
                        conn.execute("""INSERT INTO holdings(snapshot_id,instrument_id,identity_key,isin,normalized_name,display_name,asset_class,instrument_type,source_sheet,source_row,source_instrument_code,source_name,section,subsection,industry_rating,quantity,market_value_lakh,weight,ytm,ytc,direction,expiry)
                            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (snapshot_id, instrument_id, holding.identity_key, holding.isin, holding.normalized_name, holding.source_name, holding.asset_class, holding.instrument_type, snapshot.sheet_code, holding.source_row, holding.source_instrument_code, holding.source_name, holding.section, holding.subsection, holding.industry_rating, holding.quantity, holding.market_value_lakh, holding.weight, holding.ytm, holding.ytc, holding.direction, holding.expiry))
                    for issue in snapshot.issues:
                        conn.execute("INSERT INTO ingestion_issues(source_file_id,snapshot_id,severity,code,message,sheet,source_row,raw_value) VALUES(?,?,?,?,?,?,?,?)", (source_id, snapshot_id, issue.severity, issue.code, issue.message, issue.sheet, issue.row, issue.raw_value))
                    old_id = conflicts.get((fund_id, snapshot.report_date.isoformat()))
                    if old_id is not None:
                        conn.execute("UPDATE snapshots SET superseded_by_snapshot_id=? WHERE id=?", (snapshot_id, old_id))
                return SaveOutcome(source_id, "ingested", metadata)
        except (SnapshotConflictError, SourceArchiveError):
            self._discard_failed_archive(parsed.sha256, archive_entry)
            raise
        except sqlite3.Error as exc:
            self._discard_failed_archive(parsed.sha256, archive_entry)
            raise PersistenceError(f"could not persist {parsed.path.name}: {exc}") from exc

    def _discard_failed_archive(self, sha256: str, entry: ArchiveEntry | None) -> None:
        if not entry or not entry.created or not self.source_archive:
            return
        referenced = self.connection.execute("SELECT 1 FROM source_files WHERE sha256=?", (sha256,)).fetchone()
        if not referenced:
            self.source_archive.discard(entry.relative_path)

    def snapshot_frame(self, fund_id: int, report_date: str, *, include_superseded: bool = False):
        import polars as pl
        active = "" if include_superseded else " AND s.lifecycle_status='active'"
        rows = self.connection.execute(
            """SELECT h.identity_key,h.display_name,h.isin,h.asset_class,h.instrument_type,h.quantity,
            h.market_value_lakh,h.weight,h.ytm,h.ytc,h.direction,h.expiry FROM snapshots s
            JOIN holdings h ON h.snapshot_id=s.id WHERE s.fund_id=? AND s.report_date=?""" + active,
            (fund_id, report_date),
        ).fetchall()
        return pl.DataFrame([dict(row) for row in rows], strict=False, infer_schema_length=None) if rows else pl.DataFrame()

    def archive_report(self) -> dict[str, list[str]]:
        if not self.source_archive:
            raise SourceArchiveError("repository has no source archive configured")
        references = {row["sha256"]: row["archive_relpath"] for row in self.connection.execute("SELECT sha256,archive_relpath FROM source_files WHERE archive_relpath IS NOT NULL")}
        return self.source_archive.verify(references)
