# Investment Tools

This repository contains two independent local tools for working with investment
data:

| Project | Purpose | Interface |
| --- | --- | --- |
| [`mf_tracker`](./mf_tracker/) | Validate and ingest monthly mutual-fund portfolio disclosures from supported AMCs, retain immutable history, and move audited data between installations. | Python library and CLI |
| [`portfolio-manager`](./portfolio-manager/) | Upload an equity portfolio, review valuation and unrealized P&L, refresh market prices, and calculate or export rebalancing trades. | FastAPI backend and browser UI |

Each project has its own dependencies and virtual environment. They do not need
to be installed or run together.

## `mf_tracker`

`mf_tracker` processes monthly AMC portfolio workbooks. It currently supports
PPFAS, Helios, and Old Bridge formats, with structural auto-detection or an
explicit `--amc` selection. Validated snapshots are stored in SQLite with
append-only replacement history, while original workbooks are retained in a
content-addressed source archive.

### Quick start

Python 3.12 is required.

```bash
cd mf_tracker
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'

.venv/bin/mf-tracker validate path/to/workbook.xlsx --json
.venv/bin/mf-tracker ingest-file path/to/workbook.xlsx \
  --db mf_tracker.sqlite3
```

Important capabilities include:

- single-file and directory ingestion;
- PPFAS, Helios, and Old Bridge workbook adapters;
- optional, audited metadata overrides for individual files;
- dry runs and explicit append-only snapshot replacement;
- source-archive integrity verification; and
- portable ZIP bundle export and import.

See the [`mf_tracker` README](./mf_tracker/README.md) for adapter behavior,
metadata rules, persistence guarantees, and all operational commands.

## `portfolio-manager`

`portfolio-manager` is a local portfolio analysis and rebalancing application.
Its FastAPI backend owns file validation and portfolio calculations; the
framework-free browser UI handles uploads, target-weight entry, result display,
and CSV trade export.

### Quick start

Python 3.11 or newer is required. Start the backend in one terminal:

```bash
cd portfolio-manager/backend
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m uvicorn main:app --reload
```

Start the frontend in a second terminal:

```bash
cd portfolio-manager/frontend
python3 -m http.server 5173
```

Open <http://127.0.0.1:5173>. The API is available at
<http://127.0.0.1:8000>, and its interactive documentation is at
<http://127.0.0.1:8000/docs>.

The application accepts CSV, XLSX, and XLS portfolio files containing these
columns:

- `Stock Symbol`
- `Qty`
- `Avg.Price`
- `LTP`

It consolidates duplicate symbols, calculates valuation and unrealized P&L,
optionally refreshes prices through Yahoo Finance, accepts target weights and
fresh cash, applies a selectable whole-share rounding policy, and exports the
resulting buy/sell/hold plan as CSV.

See the [`portfolio-manager` README](./portfolio-manager/README.md) for input
constraints, symbol handling, rebalancing semantics, and API endpoints.

## Tests

The projects have separate test suites:

```bash
# Mutual-fund ingestion
(cd mf_tracker && .venv/bin/python -m pytest)

# Portfolio Manager backend
(cd portfolio-manager/backend && .venv/bin/python -m pytest)
```

## Scope

Both projects are local analysis tools. They do not connect to a brokerage or
place orders, and their output should be reviewed before making investment
decisions.
