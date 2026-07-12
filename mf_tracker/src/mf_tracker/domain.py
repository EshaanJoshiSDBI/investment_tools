from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Literal

import polars as pl

Severity = Literal["warning", "error"]


@dataclass(frozen=True, slots=True)
class MetadataOverrides:
    """Explicit source metadata replacements supplied by an API/CLI caller."""

    report_date: date | None = None
    fund_code: str | None = None
    fund_name: str | None = None
    amc_name: str | None = None


@dataclass(slots=True)
class IngestionIssue:
    severity: Severity
    code: str
    message: str
    sheet: str | None = None
    row: int | None = None
    raw_value: str | None = None


@dataclass(slots=True)
class Holding:
    sheet_code: str
    source_row: int
    source_instrument_code: str | None
    source_name: str
    normalized_name: str
    isin: str | None
    asset_class: str
    instrument_type: str
    section: str | None
    subsection: str | None
    industry_rating: str | None
    quantity: float | None
    market_value_lakh: float
    weight: float
    ytm: float | None = None
    ytc: float | None = None
    direction: str | None = None
    expiry: str | None = None
    identity_key: str = ""


@dataclass(slots=True)
class FundSnapshot:
    sheet_code: str
    fund_name: str
    report_date: date
    holdings: list[Holding]
    issues: list[IngestionIssue] = field(default_factory=list)
    reported_total_value_lakh: float | None = None
    reported_total_weight: float | None = None


@dataclass(slots=True)
class ParsedWorkbook:
    path: Path
    sha256: str
    file_size: int
    reader: str
    report_date: date
    snapshots: list[FundSnapshot]
    issues: list[IngestionIssue] = field(default_factory=list)
    amc_slug: str = "ppfas"
    amc_name: str = "PPFAS Mutual Fund"
    parser_version: str = "ppfas-v1"
    metadata_overrides: dict[str, dict[str, str]] = field(default_factory=dict)


@dataclass(slots=True)
class IngestionResult:
    path: str
    sha256: str
    report_date: str
    reader: str
    status: str
    snapshot_count: int
    holding_count: int
    counts_by_asset_class: dict[str, int]
    issues: list[IngestionIssue] = field(default_factory=list)
    amc_slug: str = "ppfas"
    amc_name: str = "PPFAS Mutual Fund"
    effective_metadata: dict[str, Any] = field(default_factory=dict)
    source_file_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["issues"] = [asdict(issue) for issue in self.issues]
        return result


@dataclass(slots=True)
class BatchIngestionResult:
    directory: str
    results: list[IngestionResult]
    failures: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "directory": self.directory,
            "results": [result.to_dict() for result in self.results],
            "failures": self.failures,
        }


@dataclass(slots=True)
class ComparisonResult:
    fund_id: int
    from_date: date
    to_date: date
    frame: pl.DataFrame

    def to_dicts(self) -> list[dict[str, Any]]:
        return self.frame.to_dicts()
