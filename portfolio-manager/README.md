# Portfolio Manager MVP

Local portfolio rebalancing MVP with a FastAPI backend and a plain HTML/CSS/JS frontend. No Streamlit, frontend framework, database, or build step is used.

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
source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload
```

The API runs at `http://127.0.0.1:8000`.

## Run Frontend

Serve the frontend locally (opening `index.html` directly is not supported because the API restricts CORS to the local frontend origin):

```bash
cd portfolio-manager/frontend
python -m http.server 5173
```

Then open `http://127.0.0.1:5173`.

## Expected Portfolio Columns

Mandatory MVP columns:

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

## Tests

```bash
cd portfolio-manager/backend
pytest
```
