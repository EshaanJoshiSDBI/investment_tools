# Portfolio Manager

Local portfolio analysis and rebalancing application with a FastAPI backend and
a plain HTML/CSS/JS frontend. It can import broker portfolio files, calculate
valuation and unrealized P&L, refresh prices, generate whole-share rebalancing
trades, and export those trades as CSV. No Streamlit, frontend framework,
database, or build step is used.

## Requirements

- Python 3.11+
- FastAPI
- pandas
- yfinance
- pydantic
- pytest

## Run Backend

```bash
cd portfolio-manager/backend
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m uvicorn main:app --reload
```

The API runs at `http://127.0.0.1:8000`; interactive OpenAPI documentation is
available at `http://127.0.0.1:8000/docs`.

## Run Frontend

Serve the frontend locally (opening `index.html` directly is not supported because the API restricts CORS to the local frontend origin):

```bash
cd portfolio-manager/frontend
python -m http.server 5173
```

Then open `http://127.0.0.1:5173`.

## Run with the shared Ledger workspace

Keep the backend command above running. In a second terminal, serve the
repository root rather than the frontend subdirectory:

```bash
cd ../..
python3 -m http.server 5173
```

Open `http://127.0.0.1:5173` and select Portfolio Manager. If MF Tracker is also
needed, start it in a third terminal:

```bash
cd mf_tracker
.venv/bin/mf-tracker serve --db mf_tracker.sqlite3
```

Portfolio Manager does not currently persist uploaded portfolios. Upload the
portfolio file again after restarting its backend or refreshing the browser.
MF Tracker data is independent and persists in its configured SQLite database.

## Workflow

1. Upload a CSV, XLSX, or XLS portfolio export.
2. Review market value, cost, unrealized P&L, and current weights.
3. Optionally refresh prices through Yahoo Finance.
4. Enter target weights, optional fresh cash, and a whole-share rounding mode.
5. Calculate the buy, sell, or hold plan and review its cash impact and drift.
6. Export the displayed trade plan as `rebalance-trades.csv`.

## Expected Portfolio Columns

Required columns:

- `Stock Symbol`
- `Qty`
- `Avg.Price`
- `LTP`

Other broker-export columns may exist and are ignored by the MVP.

Uploads are limited to 10 MiB and 5,000 rows. Repeated rows for the same symbol are consolidated using summed quantity and weighted average cost. Duplicate rows must have the same LTP.

Symbols may contain letters, numbers, `.`, `&`, `_`, and `-`. Price refresh treats symbols without an exchange suffix as NSE symbols and queries Yahoo Finance with `.NS`; already-suffixed symbols such as `RELIANCE.NS` or `500325.BO` are preserved. A failed symbol keeps its uploaded LTP while other successful prices are applied.

## API

- `GET /api/health`
- `POST /api/portfolio/upload`
- `POST /api/portfolio/rebalance`
- `POST /api/portfolio/refresh-prices`
- `POST /api/portfolio/summary`

`/api/portfolio/summary` is a lightweight recalculation helper used by the plain frontend after optional price refresh, so the browser does not own portfolio math.

Fresh cash is deposit-only and must be zero or positive. Target weights may total less than 100%; the remainder is held as cash and included when calculating final weights and drift.

Target weights cannot exceed 100% in total. Rebalancing supports `nearest`,
`floor`, and `ceil` whole-share rounding. The output includes total buys, total
sells, net cash required, available-cash surplus or shortfall, final weights,
and weight drift. Calculations are informational only; the application does not
connect to a brokerage or place orders.

## Tests

```bash
cd portfolio-manager/backend
.venv/bin/python -m pytest
```
