# Portfolio Manager

Local portfolio analysis and rebalancing application with a FastAPI backend and
a plain HTML/CSS/JS frontend. It can import broker portfolio files, calculate
valuation and unrealized P&L, refresh prices, generate whole-share rebalancing
trades, and export those trades as CSV. No Streamlit, frontend framework,
or build step is used. Portfolio state persists in a local SQLite database.

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

By default the backend stores data in `../../data/portfolio_manager.sqlite3`
relative to this project. Override it with `PORTFOLIO_MANAGER_DB=/path/to/db`.

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
.venv/bin/mf-tracker serve --db ../data/mf_tracker.sqlite3
```

Portfolio Manager and MF Tracker use independent SQLite databases. Portfolio
Manager reloads its active portfolio after browser or backend restarts.

## Workflow

1. Upload a CSV, XLSX, or XLS portfolio export.
2. Review market value, cost, unrealized P&L, and current weights.
3. Optionally refresh prices through Yahoo Finance.
4. Enter target weights, optional fresh cash, and a whole-share rounding mode.
5. Calculate the buy, sell, or hold plan and review its cash impact and drift.
6. Review or restore older immutable snapshots from Portfolio History.
7. Export the displayed trade plan as `rebalance-trades.csv` or download a
   portable Portfolio Manager backup.

## Persistence and backups

Each successful new upload creates an active snapshot and supersedes the previous
one without deleting it. Restoring history clones the selected snapshot into a
new active snapshot. Target weights, fresh cash, rounding mode, and successful
timestamped price refreshes are retained. P&L, weights, summaries, and rebalance
plans are recalculated and are not stored.

The database stores the normalized holdings plus the uploaded filename, size,
SHA-256, and parser version. Original broker files are not copied into storage.
The Export Backup action produces a versioned natural-key ZIP; Import Backup is
accepted only when the destination database contains no portfolio snapshots.
SQLite files and backups contain unencrypted local financial data and are ignored
by Git.

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
- `GET /api/portfolio`
- `POST /api/portfolio/upload`
- `GET /api/portfolio/snapshots/{snapshot_id}`
- `POST /api/portfolio/snapshots/{snapshot_id}/restore`
- `PUT /api/portfolio/snapshots/{snapshot_id}/working-state`
- `POST /api/portfolio/snapshots/{snapshot_id}/refresh-prices`
- `POST /api/portfolio/snapshots/{snapshot_id}/rebalance`
- `GET /api/portfolio/bundles/export`
- `POST /api/portfolio/bundles/import`

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
