from __future__ import annotations

from pathlib import Path

from dataclasses import dataclass
from datetime import date, datetime
from typing import Callable

from .amcs import helios, oldbridge, ppfas
from .domain import IngestionIssue, MetadataOverrides, ParsedWorkbook
from .errors import ValidationError
from .workbooks import read_workbook


Parser = Callable[[Path, object, MetadataOverrides | None], ParsedWorkbook]


@dataclass(frozen=True, slots=True)
class Adapter:
    slug: str
    matcher: Callable[[object], bool]
    parser: Parser
    fund_codes: frozenset[str]


ADAPTERS = (
    Adapter("ppfas", ppfas.matches_workbook, lambda p, r, _: ppfas.parse_raw_workbook(p, r), frozenset(ppfas.SHEET_CODES)),
    Adapter("helios", helios.matches_workbook, lambda p, r, _: helios.parse_raw_workbook(p, r), frozenset(helios.SHEET_CODES)),
    Adapter("oldbridge", oldbridge.matches_workbook, oldbridge.parse_raw_workbook, frozenset(oldbridge.SHEET_CODES)),
)
ADAPTER_BY_SLUG = {adapter.slug: adapter for adapter in ADAPTERS}


def _clean_override(label: str, value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        raise ValidationError(f"{label} override must not be blank")
    return cleaned


def _normalize_overrides(overrides: MetadataOverrides | None) -> MetadataOverrides | None:
    if overrides is None:
        return None
    if overrides.report_date is not None and (
        not isinstance(overrides.report_date, date) or isinstance(overrides.report_date, datetime)
    ):
        raise ValidationError("report_date override must be a datetime.date")
    return MetadataOverrides(
        report_date=overrides.report_date,
        fund_code=_clean_override("fund_code", overrides.fund_code),
        fund_name=_clean_override("fund_name", overrides.fund_name),
        amc_name=_clean_override("amc_name", overrides.amc_name),
    )


def _override(parsed: ParsedWorkbook, field: str, source: str, effective: str) -> None:
    if source == effective:
        return
    parsed.metadata_overrides[field] = {"source": source, "effective": effective}
    parsed.issues.append(IngestionIssue(
        "warning", "metadata_override",
        f"Explicit {field} override replaced workbook value {source!r} with {effective!r}",
        raw_value=source,
    ))


def _apply_overrides(parsed: ParsedWorkbook, adapter: Adapter, overrides: MetadataOverrides | None) -> ParsedWorkbook:
    if overrides is None:
        return parsed
    if (overrides.fund_code or overrides.fund_name) and len(parsed.snapshots) != 1:
        raise ValidationError("fund metadata overrides require a workbook with exactly one fund snapshot")
    if overrides.fund_code:
        code = overrides.fund_code.upper()
        if code not in adapter.fund_codes:
            raise ValidationError(f"fund_code {code!r} is not valid for AMC {adapter.slug}")
        snapshot = parsed.snapshots[0]
        _override(parsed, "fund_code", snapshot.sheet_code, code)
        snapshot.sheet_code = code
        for holding in snapshot.holdings:
            holding.sheet_code = code
    if overrides.fund_name:
        snapshot = parsed.snapshots[0]
        _override(parsed, "fund_name", snapshot.fund_name, overrides.fund_name)
        snapshot.fund_name = overrides.fund_name
    if overrides.report_date:
        source = parsed.report_date.isoformat()
        effective = overrides.report_date.isoformat()
        _override(parsed, "report_date", source, effective)
        parsed.report_date = overrides.report_date
        for snapshot in parsed.snapshots:
            snapshot.report_date = overrides.report_date
    if overrides.amc_name:
        _override(parsed, "amc_name", parsed.amc_name, overrides.amc_name)
        parsed.amc_name = overrides.amc_name
    return parsed


def parse_workbook(
    path: str | Path, *, amc: str | None = None, metadata: MetadataOverrides | None = None
) -> ParsedWorkbook:
    """Select an AMC adapter, parse the source once, and apply explicit metadata."""
    source = Path(path)
    raw = read_workbook(source)
    overrides = _normalize_overrides(metadata)
    selected_amc = "auto" if amc is None else amc.strip().lower()
    if selected_amc not in {"auto", *ADAPTER_BY_SLUG}:
        raise ValidationError(f"unsupported AMC {amc!r}; expected auto, ppfas, helios, or oldbridge")
    if selected_amc != "auto":
        adapter = ADAPTER_BY_SLUG[selected_amc]
        if not adapter.matcher(raw):
            raise ValidationError(f"{source.name}: workbook does not match {selected_amc} structure")
        return _apply_overrides(adapter.parser(source, raw, overrides), adapter, overrides)
    selected = [adapter for adapter in ADAPTERS if adapter.matcher(raw)]
    if not selected:
        raise ValidationError(f"{source.name}: no recognized AMC workbook structure")
    if len(selected) > 1:
        names = ", ".join(adapter.slug for adapter in selected)
        raise ValidationError(f"{source.name}: ambiguous AMC workbook structure ({names})")
    adapter = selected[0]
    return _apply_overrides(adapter.parser(source, raw, overrides), adapter, overrides)
