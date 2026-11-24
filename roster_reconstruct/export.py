"""Safe export writers for staging data."""

from __future__ import annotations

import csv
import json
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, TextIO

from .book import _public_cell, iter_business_rows
from .staging import StagingStore


def atomic_text_write(path: str | os.PathLike[str], writer: Callable[[TextIO], int]) -> int:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=target.parent,
        prefix=f".{target.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            count = writer(fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, target)
        return count
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def write_rows_jsonl(store: StagingStore, fh: TextIO) -> int:
    count = 0
    for row in iter_business_rows(store):
        fh.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
        fh.write("\n")
        count += 1
    return count


def write_business_rows_csv(store: StagingStore, fh: TextIO) -> int:
    """Write one spreadsheet row per business row.

    Each business column gets a text column and a ``<col>__status`` column.
    Undecidable text is exported as an empty field; the status column keeps it
    distinct from a judgeable blank cell.
    """

    col_rows = store.conn.execute(
        """
        SELECT col_id, MIN(col_no) AS col_no
        FROM cells
        GROUP BY col_id
        ORDER BY
            CASE WHEN MIN(col_no) IS NULL THEN 1 ELSE 0 END,
            MIN(col_no),
            col_id
        """
    ).fetchall()
    col_ids = [row["col_id"] for row in col_rows]

    fieldnames = ["row_id", "row_no", "page_ids"]
    for col_id in col_ids:
        fieldnames.extend([col_id, f"{col_id}__status"])

    writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    count = 0

    for row in iter_business_rows(store):
        status_by_col = {
            cell["col_id"]: cell["status"] for cell in row["cells"]
        }
        out: dict[str, Any] = {
            "row_id": row["row_id"],
            "row_no": "" if row["row_no"] is None else row["row_no"],
            "page_ids": "|".join(str(p) for p in row["page_ids"]),
        }
        for col_id in col_ids:
            value = row["text_by_col"].get(col_id)
            out[col_id] = "" if value is None else value
            out[f"{col_id}__status"] = status_by_col.get(col_id, "absent")
        writer.writerow(out)
        count += 1
    return count


def write_cells_jsonl(store: StagingStore, fh: TextIO) -> int:
    count = 0
    for cell in store.iter_cell_rows():
        public = _public_cell(cell)
        fh.write(json.dumps(public, ensure_ascii=False, separators=(",", ":")))
        fh.write("\n")
        count += 1
    return count


def write_diagnostics_jsonl(store: StagingStore, fh: TextIO) -> int:
    count = 0
    for diag in store.iter_diagnostics():
        fh.write(json.dumps(diag, ensure_ascii=False, separators=(",", ":")))
        fh.write("\n")
        count += 1
    return count


_CSV_FIELDS = [
    "cell_id",
    "row_id",
    "col_id",
    "row_no",
    "col_no",
    "reading_order",
    "page_index",
    "page_id",
    "page_no",
    "status",
    "text",
    "rect",
    "reasons",
    "evidence",
]


def write_cells_csv(store: StagingStore, fh: TextIO) -> int:
    writer = csv.DictWriter(fh, fieldnames=_CSV_FIELDS, extrasaction="ignore")
    writer.writeheader()
    count = 0
    for raw in store.iter_cell_rows():
        cell = _public_cell(raw)
        row = {key: cell.get(key) for key in _CSV_FIELDS}
        row["rect"] = json.dumps(cell.get("rect", []), separators=(",", ":"))
        if cell["status"] == "undecidable":
            row["reasons"] = json.dumps(cell.get("reasons", []), ensure_ascii=False)
            row["evidence"] = ""
            row["text"] = ""
        else:
            row["reasons"] = ""
            row["evidence"] = json.dumps(
                cell.get("evidence", []), ensure_ascii=False, separators=(",", ":")
            )
        writer.writerow(row)
        count += 1
    return count
