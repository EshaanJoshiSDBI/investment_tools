from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from portfolio.calculations import calculate_holdings, calculate_summary
from portfolio.errors import (
    DuplicateImportError,
    InactiveSnapshotError,
    MigrationError,
    PortfolioPersistenceError,
    SnapshotNotFoundError,
)
from portfolio.models import (
    Holding,
    PortfolioState,
    RoundingMode,
    SnapshotSummary,
    SourceMetadata,
    TargetWeight,
    WorkingStateRequest,
    WorkspaceState,
)
from portfolio.validation import validate_holdings, validate_target_weights


SCHEMA_VERSION = 2
PARSER_VERSION = "portfolio-upload-v1"

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migrations (
 version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS portfolio_imports (
 id INTEGER PRIMARY KEY, ingestion_key TEXT NOT NULL UNIQUE, sha256 TEXT NOT NULL,
 filename TEXT NOT NULL, file_size INTEGER NOT NULL CHECK(file_size >= 0),
 parser_version TEXT NOT NULL, imported_at TEXT NOT NULL,
 source_type TEXT NOT NULL DEFAULT 'file' CHECK(source_type IN ('file','kite')),
 source_account_ref TEXT, structural_fingerprint TEXT
);
CREATE TABLE IF NOT EXISTS portfolio_snapshots (
 id INTEGER PRIMARY KEY, snapshot_key TEXT NOT NULL UNIQUE,
 import_id INTEGER NOT NULL REFERENCES portfolio_imports(id),
 created_at TEXT NOT NULL, lifecycle_status TEXT NOT NULL DEFAULT 'active'
  CHECK(lifecycle_status IN ('active','superseded')),
 superseded_at TEXT, superseded_by_snapshot_id INTEGER REFERENCES portfolio_snapshots(id),
 restored_from_snapshot_id INTEGER REFERENCES portfolio_snapshots(id)
);
CREATE TABLE IF NOT EXISTS portfolio_holdings (
 id INTEGER PRIMARY KEY, snapshot_id INTEGER NOT NULL REFERENCES portfolio_snapshots(id),
 symbol TEXT NOT NULL, quantity REAL NOT NULL CHECK(quantity >= 0),
 avg_price REAL NOT NULL CHECK(avg_price > 0), uploaded_ltp REAL NOT NULL CHECK(uploaded_ltp > 0),
 UNIQUE(snapshot_id, symbol)
);
CREATE TABLE IF NOT EXISTS portfolio_snapshot_settings (
 snapshot_id INTEGER PRIMARY KEY REFERENCES portfolio_snapshots(id),
 fresh_cash REAL NOT NULL DEFAULT 0 CHECK(fresh_cash >= 0),
 rounding_mode TEXT NOT NULL DEFAULT 'nearest' CHECK(rounding_mode IN ('nearest','floor','ceil')),
 updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS portfolio_target_weights (
 snapshot_id INTEGER NOT NULL REFERENCES portfolio_snapshots(id), symbol TEXT NOT NULL,
 target_weight_pct REAL NOT NULL CHECK(target_weight_pct >= 0 AND target_weight_pct <= 100),
 PRIMARY KEY(snapshot_id, symbol)
);
CREATE TABLE IF NOT EXISTS portfolio_price_observations (
 id INTEGER PRIMARY KEY, snapshot_id INTEGER NOT NULL REFERENCES portfolio_snapshots(id),
 symbol TEXT NOT NULL, price REAL NOT NULL CHECK(price > 0), provider TEXT NOT NULL,
 observed_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_portfolio_active_snapshot
 ON portfolio_snapshots(lifecycle_status) WHERE lifecycle_status='active';
CREATE INDEX IF NOT EXISTS idx_portfolio_snapshots_created
 ON portfolio_snapshots(created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_portfolio_prices_latest
 ON portfolio_price_observations(snapshot_id, symbol, observed_at DESC, id DESC);
"""

MIGRATION_V2 = (
    "ALTER TABLE portfolio_imports ADD COLUMN source_type TEXT NOT NULL DEFAULT 'file' "
    "CHECK(source_type IN ('file','kite'))",
    "ALTER TABLE portfolio_imports ADD COLUMN source_account_ref TEXT",
    "ALTER TABLE portfolio_imports ADD COLUMN structural_fingerprint TEXT",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def ingestion_key(payload: bytes, parser_version: str = PARSER_VERSION) -> tuple[str, str]:
    sha256 = hashlib.sha256(payload).hexdigest()
    identity = hashlib.sha256(f"{sha256}|{parser_version}".encode()).hexdigest()
    return identity, sha256


class SQLitePortfolioRepository:
    def __init__(self, path: str | Path, *, busy_timeout_ms: int = 5000):
        self.path = Path(path)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.connection = sqlite3.connect(
                self.path, timeout=busy_timeout_ms / 1000, check_same_thread=False
            )
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA foreign_keys = ON")
            self.connection.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
            self.connection.execute("PRAGMA journal_mode = WAL")
            self.connection.execute("PRAGMA synchronous = NORMAL")
            self._migrate()
        except MigrationError:
            if hasattr(self, "connection"):
                self.connection.close()
            raise
        except (OSError, sqlite3.Error) as exc:
            if hasattr(self, "connection"):
                self.connection.close()
            raise PortfolioPersistenceError(f"Could not open portfolio database: {exc}") from exc

    def _migrate(self) -> None:
        version = self.connection.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise MigrationError(
                f"Database schema {version} is newer than supported schema {SCHEMA_VERSION}"
            )
        tables = self.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        if tables and version == 0:
            raise MigrationError("Existing unversioned database cannot be migrated safely")
        if version == 0:
            try:
                with self.connection:
                    self.connection.executescript(SCHEMA)
                    self.connection.execute(
                        "INSERT INTO schema_migrations(version,applied_at) VALUES(?,?)",
                        (SCHEMA_VERSION, utc_now()),
                    )
                    self.connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            except sqlite3.Error as exc:
                raise MigrationError(f"Could not initialize portfolio database: {exc}") from exc
        elif version == 1:
            try:
                with self.connection:
                    for statement in MIGRATION_V2:
                        self.connection.execute(statement)
                    self.connection.execute(
                        "INSERT INTO schema_migrations(version,applied_at) VALUES(?,?)",
                        (SCHEMA_VERSION, utc_now()),
                    )
                    self.connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            except sqlite3.Error as exc:
                raise MigrationError(f"Could not migrate portfolio database to v2: {exc}") from exc
            self.connection.executescript(SCHEMA)
            self.connection.commit()
        else:
            self.connection.executescript(SCHEMA)
            self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "SQLitePortfolioRepository":
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

    def is_empty(self) -> bool:
        return self.connection.execute("SELECT 1 FROM portfolio_snapshots LIMIT 1").fetchone() is None

    def save_upload(
        self, filename: str, payload: bytes, holdings: list[Holding]
    ) -> tuple[str, WorkspaceState]:
        validate_holdings(holdings)
        key, sha256 = ingestion_key(payload, PARSER_VERSION)
        existing = self._existing_import_snapshot(key)
        if existing:
            if existing["lifecycle_status"] == "active":
                return "no_op", self.workspace()
            raise DuplicateImportError(
                "This file already exists in portfolio history; restore that snapshot instead",
                existing["snapshot_key"],
            )

        now = utc_now()
        snapshot_key = str(uuid.uuid4())
        active = self._active_row()
        prior_targets: dict[str, float] = {}
        fresh_cash = 0.0
        rounding = RoundingMode.nearest
        if active:
            prior_targets = self._target_map(active["id"])
            settings = self.connection.execute(
                "SELECT fresh_cash,rounding_mode FROM portfolio_snapshot_settings WHERE snapshot_id=?",
                (active["id"],),
            ).fetchone()
            fresh_cash = float(settings["fresh_cash"])
            rounding = RoundingMode(settings["rounding_mode"])

        symbols = {holding.symbol for holding in holdings}
        if active:
            targets = {symbol: prior_targets.get(symbol, 0.0) for symbol in symbols}
        else:
            targets = _initial_targets(holdings)

        try:
            with self.transaction() as conn:
                conn.execute(
                    """INSERT INTO portfolio_imports(
                       ingestion_key,sha256,filename,file_size,parser_version,imported_at,source_type
                       ) VALUES(?,?,?,?,?,?,?)""",
                    (key, sha256, filename, len(payload), PARSER_VERSION, now, "file"),
                )
                import_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                if active:
                    conn.execute(
                        "UPDATE portfolio_snapshots SET lifecycle_status='superseded',superseded_at=? WHERE id=?",
                        (now, active["id"]),
                    )
                conn.execute(
                    "INSERT INTO portfolio_snapshots(snapshot_key,import_id,created_at) VALUES(?,?,?)",
                    (snapshot_key, import_id, now),
                )
                snapshot_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                self._insert_snapshot_data(conn, snapshot_id, holdings, targets, fresh_cash, rounding, now)
                if active:
                    conn.execute(
                        "UPDATE portfolio_snapshots SET superseded_by_snapshot_id=? WHERE id=?",
                        (snapshot_id, active["id"]),
                    )
        except sqlite3.IntegrityError as exc:
            existing = self._existing_import_snapshot(key)
            if existing:
                if existing["lifecycle_status"] == "active":
                    return "no_op", self.workspace()
                raise DuplicateImportError(
                    "This file already exists in portfolio history; restore that snapshot instead",
                    existing["snapshot_key"],
                ) from exc
            raise PortfolioPersistenceError(f"Could not persist portfolio upload: {exc}") from exc
        except sqlite3.Error as exc:
            raise PortfolioPersistenceError(f"Could not persist portfolio upload: {exc}") from exc
        return "ingested", self.workspace()

    def sync_kite(
        self,
        holdings: list[Holding],
        *,
        account_ref: str,
        structural_fingerprint: str,
        parser_version: str,
    ) -> tuple[str, WorkspaceState]:
        validate_holdings(holdings, allow_empty=True)
        active = self._active_row()
        now = utc_now()
        prices = {holding.symbol: holding.ltp for holding in holdings}

        if (
            active
            and active["source_type"] == "kite"
            and active["source_account_ref"] == account_ref
            and active["structural_fingerprint"] == structural_fingerprint
        ):
            try:
                with self.transaction() as conn:
                    conn.executemany(
                        "INSERT INTO portfolio_price_observations(snapshot_id,symbol,price,provider,observed_at) VALUES(?,?,?,?,?)",
                        [
                            (active["id"], symbol, price, "kite", now)
                            for symbol, price in prices.items()
                        ],
                    )
            except sqlite3.Error as exc:
                raise PortfolioPersistenceError(
                    f"Could not persist Kite price refresh: {exc}"
                ) from exc
            return "prices_refreshed", self.workspace()

        snapshot_key = str(uuid.uuid4())
        payload = json.dumps(
            {
                "account_ref": account_ref,
                "event_key": snapshot_key,
                "structural_fingerprint": structural_fingerprint,
                "observed_at": now,
                "prices": prices,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        key, sha256 = ingestion_key(payload, parser_version)
        prior_targets: dict[str, float] = {}
        fresh_cash = 0.0
        rounding = RoundingMode.nearest
        if active:
            prior_targets = self._target_map(active["id"])
            settings = self.connection.execute(
                "SELECT fresh_cash,rounding_mode FROM portfolio_snapshot_settings WHERE snapshot_id=?",
                (active["id"],),
            ).fetchone()
            fresh_cash = float(settings["fresh_cash"])
            rounding = RoundingMode(settings["rounding_mode"])
        symbols = {holding.symbol for holding in holdings}
        targets = (
            {symbol: prior_targets.get(symbol, 0.0) for symbol in symbols}
            if active
            else _initial_targets(holdings)
        )

        try:
            with self.transaction() as conn:
                conn.execute(
                    """INSERT INTO portfolio_imports(
                       ingestion_key,sha256,filename,file_size,parser_version,imported_at,
                       source_type,source_account_ref,structural_fingerprint
                       ) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (
                        key,
                        sha256,
                        "Zerodha Kite holdings",
                        len(payload),
                        parser_version,
                        now,
                        "kite",
                        account_ref,
                        structural_fingerprint,
                    ),
                )
                import_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                if active:
                    conn.execute(
                        "UPDATE portfolio_snapshots SET lifecycle_status='superseded',superseded_at=? WHERE id=?",
                        (now, active["id"]),
                    )
                conn.execute(
                    "INSERT INTO portfolio_snapshots(snapshot_key,import_id,created_at) VALUES(?,?,?)",
                    (snapshot_key, import_id, now),
                )
                snapshot_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                self._insert_snapshot_data(
                    conn, snapshot_id, holdings, targets, fresh_cash, rounding, now
                )
                conn.executemany(
                    "INSERT INTO portfolio_price_observations(snapshot_id,symbol,price,provider,observed_at) VALUES(?,?,?,?,?)",
                    [
                        (snapshot_id, symbol, price, "kite", now)
                        for symbol, price in prices.items()
                    ],
                )
                if active:
                    conn.execute(
                        "UPDATE portfolio_snapshots SET superseded_by_snapshot_id=? WHERE id=?",
                        (snapshot_id, active["id"]),
                    )
        except sqlite3.Error as exc:
            raise PortfolioPersistenceError(f"Could not persist Kite holdings: {exc}") from exc
        return "snapshot_created", self.workspace()

    def _insert_snapshot_data(
        self, conn: sqlite3.Connection, snapshot_id: int, holdings: list[Holding],
        targets: dict[str, float], fresh_cash: float, rounding: RoundingMode, now: str,
    ) -> None:
        conn.executemany(
            "INSERT INTO portfolio_holdings(snapshot_id,symbol,quantity,avg_price,uploaded_ltp) VALUES(?,?,?,?,?)",
            [(snapshot_id, h.symbol, h.quantity, h.avg_price, h.ltp) for h in holdings],
        )
        conn.execute(
            "INSERT INTO portfolio_snapshot_settings(snapshot_id,fresh_cash,rounding_mode,updated_at) VALUES(?,?,?,?)",
            (snapshot_id, fresh_cash, rounding.value, now),
        )
        conn.executemany(
            "INSERT INTO portfolio_target_weights(snapshot_id,symbol,target_weight_pct) VALUES(?,?,?)",
            [(snapshot_id, symbol, weight) for symbol, weight in sorted(targets.items())],
        )

    def workspace(self) -> WorkspaceState:
        active = self._active_row()
        return WorkspaceState(
            active=self.state(active["snapshot_key"]) if active else None,
            snapshots=self.list_snapshots(),
        )

    def list_snapshots(self) -> list[SnapshotSummary]:
        rows = self.connection.execute(
            """SELECT s.snapshot_key,s.created_at,s.lifecycle_status,r.snapshot_key AS restored_from,
                      i.filename
               FROM portfolio_snapshots s JOIN portfolio_imports i ON i.id=s.import_id
               LEFT JOIN portfolio_snapshots r ON r.id=s.restored_from_snapshot_id
               ORDER BY s.created_at DESC,s.id DESC"""
        ).fetchall()
        return [
            SnapshotSummary(
                snapshot_id=row["snapshot_key"], filename=row["filename"],
                created_at=row["created_at"], lifecycle_status=row["lifecycle_status"],
                restored_from_snapshot_id=row["restored_from"],
            ) for row in rows
        ]

    def state(self, snapshot_key: str) -> PortfolioState:
        row = self._snapshot_row(snapshot_key)
        prices = {
            item["symbol"]: (float(item["price"]), item["observed_at"])
            for item in self.connection.execute(
                """SELECT p.symbol,p.price,p.observed_at FROM portfolio_price_observations p
                   WHERE p.snapshot_id=? AND p.id=(
                     SELECT p2.id FROM portfolio_price_observations p2
                     WHERE p2.snapshot_id=p.snapshot_id AND p2.symbol=p.symbol
                     ORDER BY p2.observed_at DESC,p2.id DESC LIMIT 1)""",
                (row["id"],),
            )
        }
        holdings = [
            Holding(
                symbol=item["symbol"], quantity=item["quantity"], avg_price=item["avg_price"],
                ltp=prices.get(item["symbol"], (item["uploaded_ltp"], None))[0],
            )
            for item in self.connection.execute(
                "SELECT symbol,quantity,avg_price,uploaded_ltp FROM portfolio_holdings WHERE snapshot_id=? ORDER BY id",
                (row["id"],),
            )
        ]
        calculated = calculate_holdings(holdings)
        settings = self.connection.execute(
            "SELECT fresh_cash,rounding_mode FROM portfolio_snapshot_settings WHERE snapshot_id=?",
            (row["id"],),
        ).fetchone()
        source = SourceMetadata(
            filename=row["filename"], file_size=row["file_size"], sha256=row["sha256"],
            parser_version=row["parser_version"], imported_at=row["imported_at"],
            source_type=row["source_type"],
        )
        target_map = self._target_map(row["id"])
        latest = max((observed for _, observed in prices.values() if observed), default=None)
        return PortfolioState(
            snapshot_id=row["snapshot_key"], lifecycle_status=row["lifecycle_status"],
            is_active=row["lifecycle_status"] == "active", source=source,
            holdings=calculated, summary=calculate_summary(calculated),
            target_weights=[TargetWeight(symbol=h.symbol, target_weight_pct=target_map.get(h.symbol, 0)) for h in calculated],
            fresh_cash=settings["fresh_cash"], rounding_mode=RoundingMode(settings["rounding_mode"]),
            latest_price_at=latest,
        )

    def save_working_state(self, snapshot_key: str, request: WorkingStateRequest) -> PortfolioState:
        row = self._snapshot_row(snapshot_key)
        self._require_active(row)
        holdings = self._source_holdings(row["id"])
        target_map = validate_target_weights(request.target_weights, holdings)
        try:
            with self.transaction() as conn:
                conn.execute(
                    "UPDATE portfolio_snapshot_settings SET fresh_cash=?,rounding_mode=?,updated_at=? WHERE snapshot_id=?",
                    (request.fresh_cash, request.rounding_mode.value, utc_now(), row["id"]),
                )
                conn.execute("DELETE FROM portfolio_target_weights WHERE snapshot_id=?", (row["id"],))
                conn.executemany(
                    "INSERT INTO portfolio_target_weights(snapshot_id,symbol,target_weight_pct) VALUES(?,?,?)",
                    [(row["id"], symbol, weight) for symbol, weight in sorted(target_map.items())],
                )
        except sqlite3.Error as exc:
            raise PortfolioPersistenceError(f"Could not save portfolio settings: {exc}") from exc
        return self.state(snapshot_key)

    def record_prices(self, snapshot_key: str, prices: dict[str, float], provider: str = "yfinance") -> PortfolioState:
        row = self._snapshot_row(snapshot_key)
        self._require_active(row)
        symbols = {holding.symbol for holding in self._source_holdings(row["id"])}
        unknown = sorted(set(prices) - symbols)
        if unknown:
            raise PortfolioPersistenceError(f"Price results contained unknown symbols: {', '.join(unknown)}")
        if prices:
            observed_at = utc_now()
            try:
                with self.transaction() as conn:
                    conn.executemany(
                        "INSERT INTO portfolio_price_observations(snapshot_id,symbol,price,provider,observed_at) VALUES(?,?,?,?,?)",
                        [(row["id"], symbol, price, provider, observed_at) for symbol, price in prices.items()],
                    )
            except sqlite3.Error as exc:
                raise PortfolioPersistenceError(f"Could not persist refreshed prices: {exc}") from exc
        return self.state(snapshot_key)

    def restore(self, snapshot_key: str) -> WorkspaceState:
        source = self._snapshot_row(snapshot_key)
        if source["lifecycle_status"] == "active":
            return self.workspace()
        active = self._active_row()
        now = utc_now()
        new_key = str(uuid.uuid4())
        holdings = self._source_holdings(source["id"])
        targets = self._target_map(source["id"])
        settings = self.connection.execute(
            "SELECT fresh_cash,rounding_mode FROM portfolio_snapshot_settings WHERE snapshot_id=?",
            (source["id"],),
        ).fetchone()
        latest_prices = self._latest_prices(source["id"])
        try:
            with self.transaction() as conn:
                if active:
                    conn.execute(
                        "UPDATE portfolio_snapshots SET lifecycle_status='superseded',superseded_at=? WHERE id=?",
                        (now, active["id"]),
                    )
                conn.execute(
                    """INSERT INTO portfolio_snapshots(
                       snapshot_key,import_id,created_at,restored_from_snapshot_id
                       ) VALUES(?,?,?,?)""",
                    (new_key, source["import_id"], now, source["id"]),
                )
                new_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                self._insert_snapshot_data(
                    conn, new_id, holdings, targets, float(settings["fresh_cash"]),
                    RoundingMode(settings["rounding_mode"]), now,
                )
                conn.executemany(
                    "INSERT INTO portfolio_price_observations(snapshot_id,symbol,price,provider,observed_at) VALUES(?,?,?,?,?)",
                    [(new_id, symbol, price, provider, observed_at) for symbol, price, provider, observed_at in latest_prices],
                )
                if active:
                    conn.execute(
                        "UPDATE portfolio_snapshots SET superseded_by_snapshot_id=? WHERE id=?",
                        (new_id, active["id"]),
                    )
        except sqlite3.Error as exc:
            raise PortfolioPersistenceError(f"Could not restore portfolio snapshot: {exc}") from exc
        return self.workspace()

    def effective_holdings(self, snapshot_key: str, *, require_active: bool = False) -> list[Holding]:
        row = self._snapshot_row(snapshot_key)
        if require_active:
            self._require_active(row)
        return self.state(snapshot_key).holdings

    def _active_row(self) -> sqlite3.Row | None:
        return self.connection.execute(
            """SELECT s.*,i.source_type,i.source_account_ref,i.structural_fingerprint
               FROM portfolio_snapshots s JOIN portfolio_imports i ON i.id=s.import_id
               WHERE s.lifecycle_status='active'"""
        ).fetchone()

    def _existing_import_snapshot(self, key: str) -> sqlite3.Row | None:
        return self.connection.execute(
            """SELECT s.snapshot_key,s.lifecycle_status FROM portfolio_imports i
               JOIN portfolio_snapshots s ON s.import_id=i.id
               WHERE i.ingestion_key=?
               ORDER BY (s.lifecycle_status='active') DESC,s.id DESC LIMIT 1""",
            (key,),
        ).fetchone()

    def _snapshot_row(self, snapshot_key: str) -> sqlite3.Row:
        row = self.connection.execute(
            """SELECT s.*,i.filename,i.file_size,i.sha256,i.parser_version,i.imported_at,
                      i.source_type,i.source_account_ref,i.structural_fingerprint
               FROM portfolio_snapshots s JOIN portfolio_imports i ON i.id=s.import_id
               WHERE s.snapshot_key=?""",
            (snapshot_key,),
        ).fetchone()
        if not row:
            raise SnapshotNotFoundError("Portfolio snapshot was not found")
        return row

    @staticmethod
    def _require_active(row: sqlite3.Row) -> None:
        if row["lifecycle_status"] != "active":
            raise InactiveSnapshotError("Historical portfolio snapshots are read-only")

    def _source_holdings(self, snapshot_id: int) -> list[Holding]:
        return [
            Holding(symbol=row["symbol"], quantity=row["quantity"], avg_price=row["avg_price"], ltp=row["uploaded_ltp"])
            for row in self.connection.execute(
                "SELECT symbol,quantity,avg_price,uploaded_ltp FROM portfolio_holdings WHERE snapshot_id=? ORDER BY id",
                (snapshot_id,),
            )
        ]

    def _target_map(self, snapshot_id: int) -> dict[str, float]:
        return {
            row["symbol"]: float(row["target_weight_pct"])
            for row in self.connection.execute(
                "SELECT symbol,target_weight_pct FROM portfolio_target_weights WHERE snapshot_id=?",
                (snapshot_id,),
            )
        }

    def _latest_prices(self, snapshot_id: int) -> list[tuple[str, float, str, str]]:
        return [tuple(row) for row in self.connection.execute(
            """SELECT p.symbol,p.price,p.provider,p.observed_at FROM portfolio_price_observations p
               WHERE p.snapshot_id=? AND p.id=(SELECT p2.id FROM portfolio_price_observations p2
                 WHERE p2.snapshot_id=p.snapshot_id AND p2.symbol=p.symbol
                 ORDER BY p2.observed_at DESC,p2.id DESC LIMIT 1)""",
            (snapshot_id,),
        )]


def _initial_targets(holdings: list[Holding]) -> dict[str, float]:
    calculated = calculate_holdings(holdings)
    targets = {holding.symbol: round(holding.current_weight_pct, 2) for holding in calculated}
    if targets and sum(targets.values()) > 0:
        largest = max(calculated, key=lambda item: item.current_weight_pct).symbol
        targets[largest] = round(targets[largest] + 100 - sum(targets.values()), 2)
    return targets
