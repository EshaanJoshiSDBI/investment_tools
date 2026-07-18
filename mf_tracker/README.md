# MF Tracker ingestion backend

Python 3.12+ ingestion library for monthly AMC portfolio workbooks. PPFAS,
Helios, and Old Bridge source workbooks are auto-detected from their workbook structure, stored
as immutable snapshots in SQLite, and compared month to month on demand.

## Setup

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[web,dev]'
```

## Web application

Start the API and framework-free frontend together on port 5174:

```bash
.venv/bin/mf-tracker serve --db mf_tracker.sqlite3
```

Open `http://127.0.0.1:5174`, or serve the repository root on port 5173 and
open MF Tracker from the shared Ledger shell. The browser interface provides:

- a fund-by-month disclosure coverage ledger;
- searchable holdings and month-to-month comparisons with CSV export;
- validate-first single and multi-file workbook imports;
- explicit append-only snapshot replacement when an import conflicts; and
- archive verification and portable backup export.

Browser uploads accept up to 20 `.xls` or `.xlsx` files at once and 25 MiB per
file. Bundle restoration and server-side directory ingestion remain CLI-only
safeguards.

The `--db` path controls which persistent workspace is opened. When the command
above is run from this directory, records remain in `mf_tracker.sqlite3` and
original source workbooks remain in `mf_tracker.sqlite3.sources/`. Reuse the
same command after a restart to reopen the same data. Back up both locations, or
download a portable backup bundle from the Data screen.

## Usage

```bash
.venv/bin/mf-tracker validate sheets/ppfas/PPFAS_Monthly_Portfolio_Report_May_31_2026.xls
.venv/bin/mf-tracker ingest-file sheets/ppfas/PPFAS_Monthly_Portfolio_Report_May_31_2026.xls --db mf_tracker.sqlite3
.venv/bin/mf-tracker ingest-directory sheets/ppfas --db mf_tracker.sqlite3 --json
.venv/bin/mf-tracker validate sheets/helios/Helios-Flexi-Cap-Fund-Monthly-Portfolio-as-on-30th-June-2026.xlsx
.venv/bin/mf-tracker ingest-directory sheets/helios --db mf_tracker.sqlite3 --json
.venv/bin/mf-tracker validate sheets/oldbridge/OBFX_c2050b88e7.xlsx --amc oldbridge --json
.venv/bin/mf-tracker ingest-directory sheets/oldbridge --amc oldbridge --db mf_tracker.sqlite3 --dry-run
.venv/bin/mf-tracker ingest-file sheets/oldbridge/OBFE_9d7d1d029f.xlsx --amc oldbridge --db mf_tracker.sqlite3
.venv/bin/mf-tracker verify-archive --db mf_tracker.sqlite3 --json
.venv/bin/mf-tracker export-bundle --db mf_tracker.sqlite3 --output mf_tracker-backup.zip
.venv/bin/mf-tracker import-bundle mf_tracker-backup.zip --db restored.sqlite3
```

PPFAS publishes one monthly workbook containing a tab per fund. Helios publishes
one monthly `.xlsx` per fund; each file becomes one snapshot. Helios fund sheets
are stored under stable codes (`HFCF`, `HMCF`, `HSCF`, and `HFSF`) with canonical
fund names. Directory ingestion may contain any subset of funds or months.

Old Bridge also publishes one `.xlsx` per fund. Its filenames, sheet names,
header rows, and optional instrument-code column vary between reports, so the
adapter identifies fields and schemes from workbook content. The two stable fund
codes are `OBFCE` (Old Bridge Focused Fund) and `OBFLX` (Old Bridge Flexi Cap
Fund).

`--amc` is optional and defaults to structural auto-detection. Set it to
`ppfas`, `helios`, or `oldbridge` to require a particular adapter. For a single
file, source metadata can be replaced explicitly:

```bash
mf-tracker validate workbook.xlsx --amc oldbridge \
  --report-date 2026-06-30 \
  --fund-code OBFLX \
  --fund-name "Old Bridge Flexi Cap Fund" \
  --amc-name "Old Bridge Mutual Fund" \
  --json
```

Metadata replacements are applied only after the workbook structure and
holdings validate. Differences from workbook metadata are returned as
`metadata_override` warnings and persisted with the source-file record for
auditability. Fund/date overrides are intentionally unavailable for directory
ingestion because a directory can contain multiple funds and months.

The same controls are available from Python:

```python
from datetime import date

from mf_tracker import MetadataOverrides, ingest_file, parse_workbook

parsed = parse_workbook(
    "workbook.xlsx",
    amc="oldbridge",
    metadata=MetadataOverrides(report_date=date(2026, 6, 30)),
)
result = ingest_file(
    "workbook.xlsx",
    repository,
    amc="oldbridge",
    metadata=MetadataOverrides(fund_name="Old Bridge Flexi Cap Fund"),
)
```

The in-workbook portfolio date is authoritative. A conflicting date in a
filename is reported as an ingestion warning. Re-ingesting the same file hash is
a no-op only when the parser version and effective metadata also match. A changed
parser version or metadata is a new audited ingestion and an existing active
fund/date requires `--replace`.

Replacements are append-only: the old snapshot, its holdings, and its issues are
retained as superseded history while normal reads expose only the active snapshot.
Holding names and classifications are stored on each snapshot so later instrument
updates do not rewrite historical comparisons.

Persisted CLI ingestions archive the original workbook in the content-addressed
`<database>.sources` directory. Use `--source-store PATH` to choose another
location. `--dry-run` performs parsing and validation without creating or opening
the database or source archive.

SQLite schema changes are applied as ordered migrations. Databases newer than the
installed application are rejected rather than downgraded. When upgrading legacy
v1/v2 databases, holding-level historical names and classifications are backfilled
from the then-current instrument record because older schemas did not retain those
values per snapshot.

`export-bundle` writes a versioned ZIP containing natural-key JSONL data plus all
archived workbook bytes. `import-bundle` accepts only an empty destination and
verifies source hashes before restoring. This bundle, rather than SQLite row IDs,
is the stable migration boundary for future persistence backends. `verify-archive`
reports missing, corrupt, and unreferenced objects without changing them.

Excel files are selected by their byte signature, not their extension.
OOXML files use `openpyxl`; genuine legacy XLS files use `xlrd`. Polars owns all
normalization, validation, classification, and comparison transformations after
raw cell extraction.
