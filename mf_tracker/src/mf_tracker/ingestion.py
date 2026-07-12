from __future__ import annotations

from collections import Counter
from pathlib import Path

from .domain import BatchIngestionResult, IngestionResult, MetadataOverrides
from .parsing import parse_workbook
from .persistence import Repository


def ingest_file(path: str | Path, repository: Repository | None, *, dry_run: bool = False, replace: bool = False,
                amc: str | None = None, metadata: MetadataOverrides | None = None) -> IngestionResult:
    parsed = parse_workbook(path, amc=amc, metadata=metadata)
    holdings = [holding for snapshot in parsed.snapshots for holding in snapshot.holdings]
    issues = parsed.issues + [issue for snapshot in parsed.snapshots for issue in snapshot.issues]
    if dry_run:
        status = "validated"
        source_file_id = None
        stored_metadata = {
            "amc_slug": parsed.amc_slug, "amc_name": parsed.amc_name,
            "report_date": parsed.report_date.isoformat(),
            "funds": [{"fund_code": s.sheet_code, "fund_name": s.fund_name} for s in parsed.snapshots],
            "overrides": parsed.metadata_overrides,
        }
    else:
        if repository is None:
            raise ValueError("repository is required unless dry_run=True")
        outcome = repository.save_workbook(parsed, replace=replace)
        status = outcome.status
        source_file_id = outcome.source_file_id
        stored_metadata = outcome.effective_metadata
    return IngestionResult(
        path=str(parsed.path), sha256=parsed.sha256, report_date=parsed.report_date.isoformat(),
        reader=parsed.reader, status=status, snapshot_count=len(parsed.snapshots),
        holding_count=len(holdings),
        counts_by_asset_class=dict(sorted(Counter(h.asset_class for h in holdings).items())),
        issues=issues, amc_slug=parsed.amc_slug, amc_name=parsed.amc_name,
        effective_metadata=stored_metadata,
        source_file_id=source_file_id,
    )


def ingest_directory(path: str | Path, repository: Repository | None, *, pattern: str = "*.xls*", dry_run: bool = False,
                     replace: bool = False, continue_on_error: bool = False, amc: str | None = None) -> BatchIngestionResult:
    directory = Path(path)
    if not directory.exists():
        raise FileNotFoundError(f"directory does not exist: {directory}")
    if not directory.is_dir():
        raise NotADirectoryError(f"not a directory: {directory}")
    results = []
    failures: list[dict[str, str]] = []
    for source in sorted(candidate for candidate in directory.glob(pattern) if candidate.is_file()):
        try:
            results.append(ingest_file(source, repository, dry_run=dry_run, replace=replace, amc=amc))
        except Exception as exc:
            if not continue_on_error:
                raise
            failures.append({"path": str(source), "error": str(exc), "type": type(exc).__name__})
    return BatchIngestionResult(str(directory), results, failures)
