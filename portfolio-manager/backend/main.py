from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response

from portfolio.bundles import MAX_BUNDLE_BYTES, export_bundle, import_bundle
from portfolio.calculations import calculate_rebalance
from portfolio.errors import (
    BundleError,
    DuplicateImportError,
    InactiveSnapshotError,
    PortfolioPersistenceError,
    SnapshotNotFoundError,
)
from portfolio.file_loader import load_portfolio
from portfolio.models import (
    PersistedRefreshResponse,
    PortfolioState,
    RebalanceResponse,
    UploadResult,
    WorkingStateRequest,
    WorkspaceState,
)
from portfolio.persistence import SCHEMA_VERSION, SQLitePortfolioRepository
from portfolio.price_provider import PriceProvider, YFinancePriceProvider
from portfolio.validation import PortfolioValidationError, validate_target_weights


MAX_UPLOAD_BYTES = 10 * 1024 * 1024
DEFAULT_DB = Path(__file__).resolve().parents[2] / "data" / "portfolio_manager.sqlite3"


def get_price_provider() -> PriceProvider:
    return YFinancePriceProvider()


def get_repository(request: Request) -> Iterator[SQLitePortfolioRepository]:
    with SQLitePortfolioRepository(request.app.state.db_path) as repository:
        yield repository


def create_app(db_path: str | Path | None = None) -> FastAPI:
    database = Path(db_path or os.environ.get("PORTFOLIO_MANAGER_DB", DEFAULT_DB))

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        with SQLitePortfolioRepository(database):
            pass
        yield

    app = FastAPI(title="Portfolio Manager", lifespan=lifespan)
    app.state.db_path = database
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(SnapshotNotFoundError)
    async def snapshot_not_found(_: Request, exc: SnapshotNotFoundError) -> JSONResponse:
        return _error(404, "snapshot_not_found", str(exc))

    @app.exception_handler(InactiveSnapshotError)
    async def inactive_snapshot(_: Request, exc: InactiveSnapshotError) -> JSONResponse:
        return _error(409, "inactive_snapshot", str(exc))

    @app.exception_handler(DuplicateImportError)
    async def duplicate_import(_: Request, exc: DuplicateImportError) -> JSONResponse:
        return _error(409, "duplicate_import", str(exc), {"snapshot_id": exc.snapshot_id})

    @app.exception_handler(BundleError)
    async def bundle_error(_: Request, exc: BundleError) -> JSONResponse:
        return _error(400, "bundle_error", str(exc))

    @app.exception_handler(PortfolioPersistenceError)
    async def persistence_error(_: Request, exc: PortfolioPersistenceError) -> JSONResponse:
        return _error(500, "persistence_error", "Portfolio data could not be stored safely")

    @app.get("/api/health")
    def health() -> dict[str, str | int]:
        return {"status": "ok", "schema_version": SCHEMA_VERSION, "database": database.name}

    @app.get("/api/portfolio", response_model=WorkspaceState)
    def portfolio_workspace(
        repository: SQLitePortfolioRepository = Depends(get_repository),
    ) -> WorkspaceState:
        return repository.workspace()

    @app.post("/api/portfolio/upload", response_model=UploadResult)
    async def upload_portfolio(
        file: UploadFile = File(...),
        repository: SQLitePortfolioRepository = Depends(get_repository),
    ) -> UploadResult:
        content = await file.read(MAX_UPLOAD_BYTES + 1)
        if len(content) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="Portfolio file cannot exceed 10 MiB")
        try:
            parsed = load_portfolio(file.filename or "portfolio", content)
        except PortfolioValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Could not parse portfolio file: {exc}") from exc
        status, workspace = repository.save_upload(
            Path(file.filename or "portfolio").name, content, parsed.holdings
        )
        return UploadResult(status=status, workspace=workspace)

    @app.get("/api/portfolio/snapshots/{snapshot_id}", response_model=PortfolioState)
    def portfolio_snapshot(
        snapshot_id: str,
        repository: SQLitePortfolioRepository = Depends(get_repository),
    ) -> PortfolioState:
        return repository.state(snapshot_id)

    @app.post("/api/portfolio/snapshots/{snapshot_id}/restore", response_model=WorkspaceState)
    def restore_snapshot(
        snapshot_id: str,
        repository: SQLitePortfolioRepository = Depends(get_repository),
    ) -> WorkspaceState:
        return repository.restore(snapshot_id)

    @app.put("/api/portfolio/snapshots/{snapshot_id}/working-state", response_model=PortfolioState)
    def save_working_state(
        snapshot_id: str,
        working_state: WorkingStateRequest,
        repository: SQLitePortfolioRepository = Depends(get_repository),
    ) -> PortfolioState:
        try:
            return repository.save_working_state(snapshot_id, working_state)
        except PortfolioValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post(
        "/api/portfolio/snapshots/{snapshot_id}/refresh-prices",
        response_model=PersistedRefreshResponse,
    )
    def refresh_prices(
        snapshot_id: str,
        repository: SQLitePortfolioRepository = Depends(get_repository),
        provider: PriceProvider = Depends(get_price_provider),
    ) -> PersistedRefreshResponse:
        holdings = repository.effective_holdings(snapshot_id, require_active=True)
        results = provider.get_prices([holding.symbol for holding in holdings])
        successful = {
            item.symbol: item.price for item in results
            if item.success and item.price is not None
        }
        portfolio = repository.record_prices(snapshot_id, successful)
        return PersistedRefreshResponse(prices=results, portfolio=portfolio)

    @app.post(
        "/api/portfolio/snapshots/{snapshot_id}/rebalance",
        response_model=RebalanceResponse,
    )
    def rebalance_portfolio(
        snapshot_id: str,
        working_state: WorkingStateRequest,
        repository: SQLitePortfolioRepository = Depends(get_repository),
    ) -> RebalanceResponse:
        try:
            portfolio = repository.save_working_state(snapshot_id, working_state)
            target_weights = validate_target_weights(
                portfolio.target_weights, portfolio.holdings
            )
            return calculate_rebalance(
                holdings=portfolio.holdings,
                target_weights=target_weights,
                fresh_cash=portfolio.fresh_cash,
                rounding_mode=portfolio.rounding_mode,
            )
        except PortfolioValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/portfolio/bundles/export")
    def bundle_export(
        repository: SQLitePortfolioRepository = Depends(get_repository),
    ) -> Response:
        if repository.is_empty():
            raise HTTPException(status_code=404, detail="No portfolio data is available to export")
        payload = export_bundle(repository)
        return Response(
            payload,
            media_type="application/zip",
            headers={"Content-Disposition": 'attachment; filename="portfolio-manager-backup.zip"'},
        )

    @app.post("/api/portfolio/bundles/import", response_model=WorkspaceState)
    async def bundle_import(
        file: UploadFile = File(...),
        repository: SQLitePortfolioRepository = Depends(get_repository),
    ) -> WorkspaceState:
        payload = await file.read(MAX_BUNDLE_BYTES + 1)
        if len(payload) > MAX_BUNDLE_BYTES:
            raise HTTPException(status_code=413, detail="Portfolio bundle cannot exceed 100 MiB")
        import_bundle(payload, repository)
        return repository.workspace()

    return app


def _error(status: int, code: str, message: str, details: object = None) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"detail": {"code": code, "message": message, "details": details}},
    )


app = create_app()
