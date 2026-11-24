"""Streaming JSONL book processing and row export."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any, TextIO

from .engine import PageBuilder
from .errors import PageDescriptionError
from .staging import StagingStore


class BookStreamProcessor:
    """Consume page-description records without retaining a whole book."""

    def __init__(self, staging: StagingStore | None = None) -> None:
        self.staging = staging
        self.page: PageBuilder | None = None
        self.page_index = -1

    def handle(self, record: Any) -> dict[str, Any] | None:
        if not isinstance(record, dict):
            raise PageDescriptionError("invalid_record", reason="records must be objects")

        rtype = record.get("type") or record.get("record_type") or record.get("kind")
        if not isinstance(rtype, str):
            raise PageDescriptionError("invalid_record_type")

        if rtype in {"book_start", "start", "book_end"}:
            return None

        if rtype in {"page_start", "page"}:
            if self.page is not None:
                raise PageDescriptionError(
                    "page_not_closed",
                    page_id=self.page.page_id,
                    reason="start a new page only after page_end/finish",
                )
            self.page_index += 1
            page_id = record.get("page_id") or record.get("id")
            page_no = (
                record.get("page_no")
                or record.get("page_number")
                or record.get("number")
            )
            bounds = (
                record.get("bounds")
                or record.get("page_bounds")
                or record.get("page_rect")
                or record.get("rect")
                or record
            )
            self.page = PageBuilder(
                bounds,
                page_id=str(page_id) if page_id is not None else None,
                page_no=page_no,
            )

            # Convenience form: one page object containing arrays.  Only this
            # page's arrays are resident at once.
            if "cells" in record or "primitives" in record or "elements" in record:
                for cell in record.get("cells") or []:
                    self._dispatch_nested_cell(cell)
                primitives = (
                    record.get("primitives")
                    or record.get("elements")
                    or []
                )
                for prim in primitives:
                    if not isinstance(prim, dict):
                        raise PageDescriptionError("invalid_primitive_record")
                    self.page.add_record(prim)
                return self.finish()
            return None

        if rtype in {"page_end", "end_page"}:
            if self.page is None:
                raise PageDescriptionError("page_end_without_page_start")
            expected = record.get("page_id") or record.get("id")
            if expected is not None and self.page.page_id is not None:
                if str(expected) != self.page.page_id:
                    raise PageDescriptionError(
                        "page_id_mismatch",
                        expected=self.page.page_id,
                        actual=str(expected),
                    )
            return self.finish()

        if self.page is None:
            raise PageDescriptionError(
                "record_before_page_start", record_type=rtype
            )

        # PageBuilder recognizes cells/words/blanks and raises a structured
        # unsupported_layer_primitive error for any bad/unknown layer.
        self.page.add_record(record)
        return None

    def _dispatch_nested_cell(self, cell: Any) -> None:
        assert self.page is not None
        if not isinstance(cell, dict):
            raise PageDescriptionError("invalid_cell_record")
        data = dict(cell)
        data.setdefault("type", "cell")
        self.page.add_record(data)

    def finish(self) -> dict[str, Any]:
        if self.page is None:
            raise PageDescriptionError("no_active_page")
        page = self.page
        page_index = self.page_index
        # Detach before processing so a failed page does not remain reachable
        # through the streaming processor with source word strings.
        self.page = None
        try:
            result = page.finish()
        except BaseException:
            page.prims.clear()
            raise
        if self.staging is not None:
            self.staging.insert_page_result(result, page_index=page_index)
        return result

    def close(self) -> dict[str, Any] | None:
        if self.page is not None:
            return self.finish()
        return None


def process_jsonl(
    path: str | Path,
    staging: StagingStore | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield one safe page result at a time from a JSONL description.

    If *staging* is omitted, a private temporary staging database is used so
    global duplicate-cell validation still spans pages.  It is removed when
    the iterator is exhausted or closed.
    """

    own_staging = False
    if staging is None:
        fd, temp_name = tempfile.mkstemp(
            prefix="roster-stream-", suffix=".sqlite3"
        )
        os.close(fd)
        staging = StagingStore(temp_name)
        own_staging = True

    processor = BookStreamProcessor(staging=staging)
    try:
        with Path(path).open("r", encoding="utf-8") as fh:
            for line_number, line in enumerate(fh, start=1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise PageDescriptionError(
                        "invalid_json",
                        line_number=line_number,
                        reason="page description line is not valid JSON",
                    ) from exc
                try:
                    result = processor.handle(record)
                except PageDescriptionError as exc:
                    exc.details.setdefault("line_number", line_number)
                    raise
                if result is not None:
                    yield result

        final = processor.close()
        if final is not None:
            yield final
    finally:
        if own_staging:
            staging.destroy()


def process_page_description(description: dict[str, Any]) -> dict[str, Any]:
    """Process one in-memory page object and return its safe result."""

    builder = PageBuilder(
        description.get("bounds")
        or description.get("page_bounds")
        or description.get("rect")
        or description,
        page_id=description.get("page_id") or description.get("id"),
        page_no=(
            description.get("page_no")
            or description.get("page_number")
            or description.get("number")
        ),
    )
    for cell in description.get("cells") or description.get("units") or []:
        data = dict(cell)
        data.setdefault("type", "cell")
        builder.add_record(data)
    for prim in (
        description.get("primitives")
        or description.get("elements")
        or description.get("records")
        or description.get("ops")
        or []
    ):
        builder.add_record(prim)
    return builder.finish()


def _public_cell(cell: dict[str, Any]) -> dict[str, Any]:
    status = cell["status"]
    out = {
        "record_type": "cell_result",
        "cell_id": cell["cell_id"],
        "row_id": cell["row_id"],
        "col_id": cell["col_id"],
        "row_no": cell.get("row_no"),
        "col_no": cell.get("col_no"),
        "reading_order": cell.get("reading_order"),
        "page_index": cell["page_index"],
        "page_id": cell.get("page_id"),
        "page_no": cell.get("page_no"),
        "rect": cell["rect"],
        "status": status,
    }
    if status == "undecidable":
        # Deliberately no text, no evidence, no source word rectangle.
        out["reasons"] = cell.get("reasons", [])
    else:
        out["text"] = cell.get("text", "")
        out["evidence"] = cell.get("evidence", [])
    return out


def iter_business_rows(staging: StagingStore) -> Iterator[dict[str, Any]]:
    current_id: str | None = None
    current_no: int | None = None
    cells: list[dict[str, Any]] = []

    def make_row() -> dict[str, Any]:
        assert current_id is not None
        ordered = sorted(
            cells,
            key=lambda c: (
                c["page_index"],
                c["reading_order"] is None,
                c["reading_order"] if c["reading_order"] is not None else 0,
                c["col_no"] is None,
                c["col_no"] if c["col_no"] is not None else 0,
                c["col_id"],
                c["cell_id"],
            ),
        )
        public_cells = [_public_cell(c) for c in ordered]
        text_by_col: dict[str, str | None] = {}
        page_ids: list[Any] = []
        for c in public_cells:
            text_by_col[c["col_id"]] = (
                None if c["status"] == "undecidable" else c["text"]
            )
            if c.get("page_id") is not None and c["page_id"] not in page_ids:
                page_ids.append(c["page_id"])

        row_no = current_no
        if row_no is None:
            nos = [c.get("row_no") for c in ordered if c.get("row_no") is not None]
            if nos:
                row_no = nos[0]
        return {
            "record_type": "business_row",
            "row_id": current_id,
            "row_no": row_no,
            "page_ids": page_ids,
            "text_by_col": text_by_col,
            "cells": public_cells,
        }

    for cell in staging.iter_cell_rows():
        rid = cell["row_id"]
        if current_id is None:
            current_id = rid
            current_no = cell.get("row_no")
            cells = [cell]
        elif rid == current_id:
            if current_no is None and cell.get("row_no") is not None:
                current_no = cell["row_no"]
            cells.append(cell)
        else:
            yield make_row()
            current_id = rid
            current_no = cell.get("row_no")
            cells = [cell]

    if current_id is not None:
        yield make_row()


def write_jsonl(records: Iterator[dict[str, Any]] | list[dict[str, Any]], fh: TextIO) -> int:
    count = 0
    for record in records:
        fh.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
        fh.write("\n")
        count += 1
    return count
