# Portfolio Manager

Local portfolio analysis and rebalancing application with a FastAPI backend and
a plain HTML/CSS/JS frontend. It can import broker portfolio files or manually
pull long-term equity holdings from Zerodha Kite, calculate
valuation and unrealized P&L, refresh prices, generate whole-share rebalancing
trades, and export those trades as CSV. No Streamlit, frontend framework,
or build step is used. Portfolio state persists in a local SQLite database.

## Requirements

- Python 3.11+
- FastAPI
- pandas
- yfinance
- pydantic
- Kite Connect (optional; required only for Zerodha imports)
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

### Optional Zerodha Kite setup

Create a Kite Connect app and register this exact redirect URL:

```text
http://127.0.0.1:8000/api/kite/callback
```

Copy `.env.example` to `.env` in the `portfolio-manager` directory and fill in
the credentials issued for that app:

```dotenv
KITE_API_KEY=your_api_key
KITE_API_SECRET=your_api_secret
```

The real `.env` is ignored by Git. The API secret stays in the backend, and the
daily access token is held only in backend memory: it is not written to SQLite,
returned to the browser, or included in backups. A backend restart, explicit
disconnect, or Kite session expiry requires connecting again. Kite access tokens
expire by 6 AM on the following day.

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

1. Upload a CSV, XLSX, or XLS portfolio export, or connect and explicitly import
   Zerodha Kite holdings.
2. Review market value, cost, unrealized P&L, and current weights.
3. Refresh file-sourced prices through Yahoo Finance, or manually refresh a
   Kite-sourced portfolio through Kite.
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

The database stores normalized holdings plus source provenance, source hash, and
parser version. Original broker files and raw Kite responses are not copied into
storage.
The Export Backup action produces a versioned natural-key ZIP; Import Backup is
accepted only when the destination database contains no portfolio snapshots.
SQLite files and backups contain unencrypted local financial data and are ignored
by Git. They never contain Kite API credentials, access tokens, or login state.

## Zerodha Kite holdings

Kite integration is read-only and manual. `Connect Kite` completes Zerodha's
login flow but does not import anything; use `Import from Kite` or `Refresh from
Kite` explicitly. The integration calls only the long-term equity holdings API.
It does not read positions or Coin mutual funds and cannot place, modify,
authorize, or exit orders.

Imported quantity includes settled, T1, pledged collateral, and MTF quantities.
MTF and regular acquisition costs are combined using a quantity-weighted average.
NSE instruments retain a bare symbol such as `INFY`; other venues are qualified,
for example `BSE:SBIN`, so exchange legs are never merged accidentally.

A structural change to instruments, quantities, average costs, account, or source
creates an immutable snapshot. Price-only refreshes append timestamped Kite price
observations to the active snapshot without adding history noise. A valid empty
Kite response creates an empty snapshot with zero totals and disabled rebalancing.

Kite refresh is atomic. A discrepancy, invalid applicable acquisition price,
unsupported exchange, duplicate canonical identity, or missing positive last/close
price rejects the full refresh and leaves the active snapshot unchanged.

## Expected Portfolio Columns

Required columns:

- `Stock Symbol`
- `Qty`
- `Avg.Price`
- `LTP`

Other broker-export columns may exist and are ignored by the MVP.

Uploads are limited to 10 MiB and 5,000 rows. Repeated rows for the same symbol are consolidated using summed quantity and weighted average cost. Duplicate rows must have the same LTP.

Symbols may contain letters, numbers, `.`, `&`, `_`, `-`, and one `:` exchange
separator. File price refresh treats symbols without an exchange suffix as NSE
symbols and queries Yahoo Finance with `.NS`; already-suffixed symbols such as
`RELIANCE.NS` or `500325.BO` are preserved. A failed symbol keeps its uploaded
LTP while other successful prices are applied.

## API

- `GET /api/health`
- `GET /api/portfolio`
- `GET /api/kite/status`
- `POST /api/kite/session`
- `GET /api/kite/callback`
- `DELETE /api/kite/session`
- `POST /api/kite/holdings/sync`
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
and weight drift. Calculations are informational only. The optional Kite
integration reads holdings but never places or changes orders.

## Tests

```bash
cd portfolio-manager/backend
.venv/bin/python -m pytest
```
