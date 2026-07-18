from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware

from portfolio.calculations import calculate_holdings, calculate_rebalance, calculate_summary
from portfolio.file_loader import load_portfolio
from portfolio.models import (
    Holding,
    PortfolioUploadResponse,
    RebalanceRequest,
    RebalanceResponse,
    RefreshPricesRequest,
    RefreshPricesResponse,
)
from portfolio.price_provider import PriceProvider, YFinancePriceProvider
from portfolio.validation import (
    PortfolioValidationError,
    validate_holdings,
    validate_target_weights,
)


app = FastAPI(title="Portfolio Manager MVP")
MAX_UPLOAD_BYTES = 10 * 1024 * 1024

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/portfolio/upload", response_model=PortfolioUploadResponse)
async def upload_portfolio(file: UploadFile = File(...)) -> PortfolioUploadResponse:
    try:
        content = await file.read(MAX_UPLOAD_BYTES + 1)
        if len(content) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="Portfolio file cannot exceed 10 MiB")
        return load_portfolio(file.filename or "", content)
    except HTTPException:
        raise
    except PortfolioValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not parse portfolio file: {exc}") from exc


@app.post("/api/portfolio/rebalance", response_model=RebalanceResponse)
def rebalance_portfolio(request: RebalanceRequest) -> RebalanceResponse:
    try:
        validate_holdings(request.holdings)
        target_weights = validate_target_weights(request.target_weights, request.holdings)
        return calculate_rebalance(
            holdings=request.holdings,
            target_weights=target_weights,
            fresh_cash=request.fresh_cash,
            rounding_mode=request.rounding_mode,
        )
    except PortfolioValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def get_price_provider() -> PriceProvider:
    return YFinancePriceProvider()


@app.post("/api/portfolio/refresh-prices", response_model=RefreshPricesResponse)
def refresh_prices(
    request: RefreshPricesRequest,
    provider: PriceProvider = Depends(get_price_provider),
) -> RefreshPricesResponse:
    return RefreshPricesResponse(prices=provider.get_prices(request.symbols))


@app.post("/api/portfolio/summary", response_model=PortfolioUploadResponse)
def summarize_portfolio(holdings: list[Holding]) -> PortfolioUploadResponse:
    try:
        validate_holdings(holdings)
    except PortfolioValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    calculated = calculate_holdings(holdings)
    return PortfolioUploadResponse(holdings=calculated, summary=calculate_summary(calculated))
