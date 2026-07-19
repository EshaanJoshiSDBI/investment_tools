from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import date

from .errors import MfTrackerError
from .bundles import export_bundle, import_bundle, verify_archive
from .domain import MetadataOverrides
from .ingestion import ingest_directory, ingest_file
from .parsing import parse_workbook
from .persistence import SQLiteRepository, SourceArchive


def _emit(value: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(value, indent=2, default=str))
        return
    for key, item in value.items():
        if key != "issues":
            print(f"{key}: {item}")
    issues = value.get("issues", [])
    if issues:
        print(f"issues: {len(issues)}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mf-tracker")
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate")
    validate.add_argument("path")
    validate.add_argument("--json", action="store_true")
    _add_amc_argument(validate)
    _add_metadata_arguments(validate)
    for name in ("ingest-file", "ingest-directory"):
        command = commands.add_parser(name)
        command.add_argument("path")
        command.add_argument("--db", required=True)
        command.add_argument("--source-store")
        command.add_argument("--dry-run", action="store_true")
        command.add_argument("--replace", action="store_true")
        command.add_argument("--json", action="store_true")
        _add_amc_argument(command)
        if name == "ingest-file":
            _add_metadata_arguments(command)
        if name == "ingest-directory":
            command.add_argument("--pattern", default="*.xls*")
            command.add_argument("--continue-on-error", action="store_true")
    export = commands.add_parser("export-bundle")
    export.add_argument("--db", required=True)
    export.add_argument("--output", required=True)
    export.add_argument("--source-store")
    export.add_argument("--json", action="store_true")
    import_command = commands.add_parser("import-bundle")
    import_command.add_argument("bundle")
    import_command.add_argument("--db", required=True)
    import_command.add_argument("--source-store")
    import_command.add_argument("--json", action="store_true")
    verify = commands.add_parser("verify-archive")
    verify.add_argument("--db", required=True)
    verify.add_argument("--source-store")
    verify.add_argument("--json", action="store_true")
    serve = commands.add_parser("serve")
    serve.add_argument("--db", required=True)
    serve.add_argument("--source-store")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=5174)
    return parser


def _date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD") from exc


def _add_amc_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--amc", choices=("auto", "ppfas", "helios", "oldbridge", "trust"), default="auto")


def _add_metadata_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--report-date", type=_date)
    parser.add_argument("--fund-code")
    parser.add_argument("--fund-name")
    parser.add_argument("--amc-name")


def _metadata(args: argparse.Namespace) -> MetadataOverrides:
    return MetadataOverrides(
        report_date=args.report_date, fund_code=args.fund_code,
        fund_name=args.fund_name, amc_name=args.amc_name,
    )


def _archive(db: str, configured: str | None) -> SourceArchive:
    return SourceArchive(configured or f"{db}.sources")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "serve":
            try:
                import uvicorn
                from .web.main import create_app
            except ImportError as exc:
                raise MfTrackerError(
                    "web dependencies are not installed; run pip install -e '.[web]'"
                ) from exc
            uvicorn.run(
                create_app(args.db, args.source_store), host=args.host, port=args.port
            )
            return 0
        if args.command == "validate":
            parsed = parse_workbook(args.path, amc=args.amc, metadata=_metadata(args))
            holdings = [holding for snapshot in parsed.snapshots for holding in snapshot.holdings]
            value = {"path": str(parsed.path), "status": "valid", "reader": parsed.reader,
                     "amc_slug": parsed.amc_slug, "amc_name": parsed.amc_name,
                     "parser_version": parsed.parser_version,
                     "report_date": parsed.report_date.isoformat(), "snapshot_count": len(parsed.snapshots),
                     "holding_count": len(holdings),
                     "funds": [{"fund_code": s.sheet_code, "fund_name": s.fund_name} for s in parsed.snapshots],
                     "metadata_overrides": parsed.metadata_overrides,
                     "issues": [asdict(issue) for issue in parsed.issues + [
                         issue for snapshot in parsed.snapshots for issue in snapshot.issues
                     ]]}
            _emit(value, args.json)
            return 0
        if args.command in {"ingest-file", "ingest-directory"} and args.dry_run:
            if args.command == "ingest-file":
                result = ingest_file(args.path, None, dry_run=True, replace=args.replace,
                                     amc=args.amc, metadata=_metadata(args))
            else:
                result = ingest_directory(args.path, None, pattern=args.pattern, dry_run=True,
                                          replace=args.replace, continue_on_error=args.continue_on_error,
                                          amc=args.amc)
            _emit(result.to_dict(), args.json)
            return 0
        with SQLiteRepository(args.db, source_archive=_archive(args.db, args.source_store)) as repository:
            if args.command == "ingest-file":
                result = ingest_file(args.path, repository, dry_run=args.dry_run, replace=args.replace,
                                     amc=args.amc, metadata=_metadata(args))
            elif args.command == "ingest-directory":
                result = ingest_directory(args.path, repository, pattern=args.pattern, dry_run=args.dry_run,
                                          replace=args.replace, continue_on_error=args.continue_on_error,
                                          amc=args.amc)
            elif args.command == "export-bundle":
                value = export_bundle(repository, args.output)
                value["output"] = args.output
                _emit(value, args.json)
                return 0
            elif args.command == "import-bundle":
                value = import_bundle(args.bundle, repository)
                value["database"] = args.db
                _emit(value, args.json)
                return 0
            else:
                value = verify_archive(repository)
                _emit(value, args.json)
                return 1 if any(value.values()) else 0
            _emit(result.to_dict(), args.json)
            return 0
    except (MfTrackerError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
