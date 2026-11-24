"""Command line interface for roster reconstruction."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from .errors import RosterError
from .export import (
    atomic_text_write,
    write_business_rows_csv,
    write_cells_csv,
    write_cells_jsonl,
    write_diagnostics_jsonl,
    write_rows_jsonl,
)
from .fixtures import (
    write_bad_fixtures,
    write_canonical_book,
    write_large_book,
)
from .staging import StagingStore
from .book import process_jsonl


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="roster-reconstruct",
        description=(
            "Reconstruct visible roster rows from integer half-open vector "
            "page descriptions. No PDF, image, or OCR processing."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    rec = sub.add_parser("reconstruct", help="reconstruct a JSONL page book")
    rec.add_argument("input", help="path to JSONL page description")
    rec.add_argument(
        "--rows-out",
        default="-",
        help="business-row output: JSONL, wide CSV if .csv, or '-' for JSONL stdout",
    )
    rec.add_argument("--cells-out", help="optional safe cell output (.jsonl or .csv)")
    rec.add_argument("--diag-out", help="optional safe diagnostics JSONL output")
    rec.add_argument("--staging", help="optional SQLite staging path")
    rec.add_argument(
        "--keep-staging",
        action="store_true",
        help="retain the privacy-safe staging database for audit",
    )
    rec.set_defaults(handler=_cmd_reconstruct)

    fix = sub.add_parser("fixture", help="generate deterministic test fixtures")
    fix.add_argument("--out", required=True, help="output directory")
    fix.add_argument(
        "--kind",
        choices=("canonical", "large", "bad", "all"),
        default="canonical",
    )
    fix.add_argument("--pages", type=int, default=20)
    fix.add_argument("--rows", type=int, default=60)
    fix.add_argument("--cols", type=int, default=6)
    fix.add_argument("--words-per-cell", type=int, default=4)
    fix.add_argument("--seed", type=int, default=20261124)
    fix.set_defaults(handler=_cmd_fixture)
    return parser


def _open_staging(args: argparse.Namespace) -> tuple[StagingStore, bool]:
    if args.staging:
        path = Path(args.staging)
        if path.exists():
            raise RosterError(
                "staging_path_exists",
                path=str(path),
                reason="choose a new staging path or remove the existing file",
            )
        store = StagingStore(path)
        return store, bool(args.keep_staging)

    fd, name = tempfile.mkstemp(
        prefix="roster-staging-", suffix=".sqlite3"
    )
    os.close(fd)
    return StagingStore(name), bool(args.keep_staging)


def _cmd_reconstruct(args: argparse.Namespace) -> int:
    store: StagingStore | None = None
    retain = False
    try:
        store, retain = _open_staging(args)
        page_count = 0
        undecidable = 0
        hidden = 0
        partial = 0

        for result in process_jsonl(args.input, staging=store):
            page_count += 1
            stats = result.get("stats", {})
            undecidable += int(stats.get("undecidable_cell_count", 0))
            hidden += int(stats.get("fully_hidden_word_count", 0))
            partial += int(stats.get("partially_occluded_word_count", 0))

        if args.rows_out == "-":
            row_count = write_rows_jsonl(store, sys.stdout)
        elif args.rows_out.lower().endswith(".csv"):
            row_count = atomic_text_write(
                args.rows_out, lambda fh: write_business_rows_csv(store, fh)
            )
        else:
            row_count = atomic_text_write(
                args.rows_out, lambda fh: write_rows_jsonl(store, fh)
            )

        if args.cells_out:
            cells_path = args.cells_out
            if cells_path.lower().endswith(".csv"):
                atomic_text_write(cells_path, lambda fh: write_cells_csv(store, fh))
            else:
                atomic_text_write(
                    cells_path, lambda fh: write_cells_jsonl(store, fh)
                )

        if args.diag_out:
            atomic_text_write(
                args.diag_out, lambda fh: write_diagnostics_jsonl(store, fh)
            )

        summary = {
            "status": "ok",
            "pages": page_count,
            "business_rows": row_count,
            "undecidable_cells": undecidable,
            "fully_hidden_words": hidden,
            "partially_occluded_words": partial,
            "staging_retained": retain,
        }
        if retain:
            store.conn.commit()
            summary["staging_path"] = store.path
        print(json.dumps(summary, ensure_ascii=False), file=sys.stderr)
        return 0

    except RosterError as exc:
        error = exc.to_dict()
        if store is not None and args.keep_staging:
            error["details"] = error.get("details", {})
            error["details"]["staging_path"] = store.path
        print(json.dumps(error, ensure_ascii=False), file=sys.stderr)
        return 2
    except OSError as exc:
        error = {
            "status": "error",
            "error_code": "io_error",
            "exception_type": type(exc).__name__,
            "path": exc.filename,
        }
        print(json.dumps(error, ensure_ascii=False), file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - safe CLI boundary
        # Never print a raw traceback or exception message: an unusual parser
        # or driver exception could echo source data.
        error = {
            "status": "error",
            "error_code": "internal_error",
            "exception_type": type(exc).__name__,
        }
        print(json.dumps(error, ensure_ascii=False), file=sys.stderr)
        return 1
    finally:
        if store is not None:
            if retain:
                store.close()
            else:
                store.destroy()


def _cmd_fixture(args: argparse.Namespace) -> int:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    generated: dict[str, Any] = {"status": "ok", "files": []}

    if args.kind in {"canonical", "all"}:
        path = write_canonical_book(out / "canonical_book.jsonl")
        generated["files"].append(str(path))
        generated["files"].append(str(path.with_name("expected.json")))

    if args.kind in {"bad", "all"}:
        paths = write_bad_fixtures(out / "bad")
        generated["files"].extend(str(p) for p in paths.values())

    if args.kind in {"large", "all"}:
        path = write_large_book(
            out / "large_book.jsonl",
            pages=args.pages,
            rows_per_page=args.rows,
            cols=args.cols,
            words_per_cell=args.words_per_cell,
            seed=args.seed,
        )
        generated["files"].append(str(path))

    print(json.dumps(generated, ensure_ascii=False), file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
