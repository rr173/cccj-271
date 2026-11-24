"""Privacy-preserving disk staging for large books.

Only finalized, safe cell records are written here.  Undecidable cells contain
neither source word text nor word rectangles; their JSON payload has only
geometry reason codes.  The database therefore never contains a covered word's
text or box.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .errors import DuplicateCellError, PageDescriptionError

_SCHEMA = """
PRAGMA journal_mode = MEMORY;
PRAGMA synchronous = OFF;

CREATE TABLE IF NOT EXISTS pages (
    page_index INTEGER PRIMARY KEY,
    page_id TEXT,
    page_no INTEGER,
    stats_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cells (
    cell_id TEXT PRIMARY KEY,
    row_id TEXT NOT NULL,
    col_id TEXT NOT NULL,
    page_index INTEGER NOT NULL,
    page_id TEXT,
    page_no INTEGER,
    row_no INTEGER,
    col_no INTEGER,
    reading_order INTEGER,
    rect_json TEXT NOT NULL,
    status TEXT NOT NULL,
    text TEXT,
    evidence_json TEXT,
    reasons_json TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_cells_row_col
    ON cells(row_id, col_id);

CREATE TABLE IF NOT EXISTS diagnostics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    page_index INTEGER,
    page_id TEXT,
    page_no INTEGER,
    diagnostic_json TEXT NOT NULL
);
"""


class StagingStore:
    def __init__(self, path: str | os.PathLike[str] = ":memory:") -> None:
        self.path = ":memory:" if str(path) == ":memory:" else os.fspath(path)
        if self.path != ":memory:":
            parent = Path(self.path).expanduser().parent
            parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self.conn.commit()
        self._row_no_cache: dict[str, int | None] = {}
        self._col_no_cache: dict[str, int | None] = {}

    def close(self) -> None:
        self.conn.close()

    def destroy(self) -> None:
        path = self.path
        self.close()
        if path != ":memory:":
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass

    def __enter__(self) -> "StagingStore":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _resolve_row_no(self, row_id: str, row_no: int | None) -> int | None:
        if row_id not in self._row_no_cache:
            row = self.conn.execute(
                "SELECT row_no FROM cells WHERE row_id = ? "
                "AND row_no IS NOT NULL LIMIT 1",
                (row_id,),
            ).fetchone()
            self._row_no_cache[row_id] = None if row is None else row["row_no"]

        known = self._row_no_cache[row_id]
        if known is None and row_no is not None:
            self.conn.execute(
                "UPDATE cells SET row_no = ? WHERE row_id = ? "
                "AND row_no IS NULL",
                (row_no, row_id),
            )
            self._row_no_cache[row_id] = row_no
            return row_no
        if known is not None:
            if row_no is not None and row_no != known:
                raise PageDescriptionError(
                    "inconsistent_business_row_number",
                    row_id=row_id,
                    expected=known,
                    actual=row_no,
                )
            return known
        return row_no

    def _resolve_col_no(self, col_id: str, col_no: int | None) -> int | None:
        if col_id not in self._col_no_cache:
            row = self.conn.execute(
                "SELECT col_no FROM cells WHERE col_id = ? "
                "AND col_no IS NOT NULL LIMIT 1",
                (col_id,),
            ).fetchone()
            self._col_no_cache[col_id] = None if row is None else row["col_no"]

        known = self._col_no_cache[col_id]
        if known is None and col_no is not None:
            self.conn.execute(
                "UPDATE cells SET col_no = ? WHERE col_id = ? "
                "AND col_no IS NULL",
                (col_no, col_id),
            )
            self._col_no_cache[col_id] = col_no
            return col_no
        if known is not None:
            if col_no is not None and col_no != known:
                raise PageDescriptionError(
                    "inconsistent_business_column_number",
                    col_id=col_id,
                    expected=known,
                    actual=col_no,
                )
            return known
        return col_no

    def insert_page_result(
        self, result: dict[str, Any], *, page_index: int
    ) -> None:
        page_id = result.get("page_id")
        page_no = result.get("page_no")

        existing_page = self.conn.execute(
            "SELECT 1 FROM pages WHERE page_index = ? OR "
            "(page_id IS NOT NULL AND page_id = ?)",
            (page_index, page_id),
        ).fetchone()
        if existing_page is not None:
            raise PageDescriptionError(
                "duplicate_page", page_index=page_index, page_id=page_id
            )

        self.conn.execute(
            "INSERT INTO pages(page_index, page_id, page_no, stats_json) "
            "VALUES (?, ?, ?, ?)",
            (
                page_index,
                page_id,
                page_no,
                json.dumps(result.get("stats", {}), ensure_ascii=False),
            ),
        )

        for cell in result.get("cells", []):
            cid = cell["cell_id"]
            rid = cell["row_id"]
            col = cell["col_id"]
            status = cell["status"]
            row_no = self._resolve_row_no(rid, cell.get("row_no"))
            col_no = self._resolve_col_no(col, cell.get("col_no"))

            if status == "undecidable":
                # Defensive privacy boundary: never persist text/evidence for
                # an undecidable cell even if a caller accidentally supplied it.
                text = None
                evidence = None
            else:
                text = cell.get("text", "")
                evidence = json.dumps(
                    cell.get("evidence", []), ensure_ascii=False, separators=(",", ":")
                )

            params = (
                cid,
                rid,
                col,
                page_index,
                page_id,
                page_no,
                row_no,
                col_no,
                cell.get("reading_order"),
                json.dumps(cell.get("rect", []), separators=(",", ":")),
                status,
                text,
                evidence,
                json.dumps(
                    cell.get("reasons", []),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )
            try:
                self.conn.execute(
                    """
                    INSERT INTO cells (
                        cell_id, row_id, col_id, page_index, page_id, page_no,
                        row_no, col_no, reading_order, rect_json, status, text,
                        evidence_json, reasons_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    params,
                )
            except sqlite3.IntegrityError:
                # The failed INSERT is rolled back by SQLite; earlier inserts in
                # this transaction remain visible to the diagnostic queries.
                dup_id = self.conn.execute(
                    "SELECT page_id FROM cells WHERE cell_id = ?", (cid,)
                ).fetchone()
                if dup_id is not None:
                    raise DuplicateCellError(
                        conflict="cell_id",
                        cell_id=cid,
                        existing_page_id=dup_id["page_id"],
                        new_page_id=page_id,
                    ) from None
                dup_pair = self.conn.execute(
                    "SELECT cell_id, page_id FROM cells "
                    "WHERE row_id = ? AND col_id = ?",
                    (rid, col),
                ).fetchone()
                if dup_pair is not None:
                    raise DuplicateCellError(
                        conflict="row_column",
                        row_id=rid,
                        col_id=col,
                        existing_cell_id=dup_pair["cell_id"],
                        existing_page_id=dup_pair["page_id"],
                        new_page_id=page_id,
                    ) from None
                raise

        for diag in result.get("diagnostics", []):
            self.conn.execute(
                "INSERT INTO diagnostics(page_index, page_id, page_no, "
                "diagnostic_json) VALUES (?, ?, ?, ?)",
                (
                    page_index,
                    page_id,
                    page_no,
                    json.dumps(diag, ensure_ascii=False),
                ),
            )

        self.conn.commit()

    def iter_cell_rows(self, *, batch_size: int = 1000) -> Iterator[dict[str, Any]]:
        cursor = self.conn.execute(
            """
            SELECT
                c.*,
                MIN(c.row_no) OVER row_window AS resolved_row_no,
                MIN(c.reading_order) OVER row_window AS row_reading_order,
                MIN(c.page_index) OVER row_window AS row_first_page
            FROM cells AS c
            WINDOW row_window AS (PARTITION BY c.row_id)
            ORDER BY
                CASE WHEN resolved_row_no IS NULL THEN 1 ELSE 0 END,
                resolved_row_no,
                CASE WHEN row_reading_order IS NULL THEN 1 ELSE 0 END,
                row_reading_order,
                row_first_page,
                c.row_id,
                c.page_index,
                CASE WHEN c.reading_order IS NULL THEN 1 ELSE 0 END,
                c.reading_order,
                CASE WHEN c.col_no IS NULL THEN 1 ELSE 0 END,
                c.col_no,
                c.col_id,
                c.cell_id
            """
        )
        while True:
            rows = cursor.fetchmany(batch_size)
            if not rows:
                break
            for row in rows:
                yield self._decode_cell(row)

    def iter_diagnostics(self) -> Iterator[dict[str, Any]]:
        cursor = self.conn.execute(
            "SELECT diagnostic_json FROM diagnostics ORDER BY id"
        )
        while True:
            rows = cursor.fetchmany(1000)
            if not rows:
                break
            for row in rows:
                yield json.loads(row["diagnostic_json"])

    def iter_page_stats(self) -> Iterator[dict[str, Any]]:
        cursor = self.conn.execute("SELECT * FROM pages ORDER BY page_index")
        while True:
            rows = cursor.fetchmany(1000)
            if not rows:
                break
            for row in rows:
                item = dict(row)
                item["stats"] = json.loads(item.pop("stats_json"))
                yield item

    @staticmethod
    def _decode_cell(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["rect"] = json.loads(item.pop("rect_json"))
        evidence_raw = item.pop("evidence_json")
        item["evidence"] = json.loads(evidence_raw) if evidence_raw else []
        reasons_raw = item.pop("reasons_json")
        item["reasons"] = json.loads(reasons_raw) if reasons_raw else []
        return item
