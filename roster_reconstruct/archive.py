"""Persistent, versioned roster archive with single-cell lookups.

The archive is a bitemporal store:

* business/valid time: the roster's ``effective_date`` (``YYYY-MM-DD``);
* ingest/transaction time: the strictly increasing positive ``batch_no``.

Every successful ingest is one batch.  A ``submit`` batch installs a whole
roster for one effective date; a later submit for the same date replaces it
wholesale; a ``retract`` batch removes the date's currently effective roster.
Effective dates therefore need not follow ingest order.

For each business cell ``(row_id, col_id)`` the archive keeps a *persistent*
ordered map ``effective_date -> safe fact``.  A new root is stored for every
batch that affects the cell.  A lookup at cutoff batch ``C`` first locates the
newest root with ``batch_no <= C`` (one indexed lookup) and then runs a
predecessor search for the newest date not later than the requested business
date (one tree descent).  There is no version replay and no scan of the cell's
history.

Only privacy-safe cell results produced by the reconstruction engine ever enter
this database.  ``undecidable`` cells carry no text and no evidence; covered
source words are never copied to archive rows, temporary files, or output.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from .book import process_jsonl
from .errors import ArchiveError
from .staging import StagingStore

_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_PRIORITY_MASK = (1 << 63) - 1
_UNSET = object()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS archive_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- One row per accepted ingest.  Rejected ingests leave no row.
CREATE TABLE IF NOT EXISTS batches (
    batch_no INTEGER PRIMARY KEY,
    effective_date TEXT NOT NULL,
    action TEXT NOT NULL CHECK(action IN ('submit', 'retract')),
    cell_count INTEGER NOT NULL
);

-- Stable integer identity for a business cell (row_id, col_id).
CREATE TABLE IF NOT EXISTS business_cells (
    cell_key_id INTEGER PRIMARY KEY AUTOINCREMENT,
    row_id TEXT NOT NULL,
    col_id TEXT NOT NULL,
    UNIQUE(row_id, col_id)
);

-- Current roster membership per effective date.  Used only to apply a
-- wholesale replacement or retraction, never by a cutoff lookup.  Old
-- membership is captured by the persistent per-cell maps.
CREATE TABLE IF NOT EXISTS date_cells (
    effective_date TEXT NOT NULL,
    cell_key_id INTEGER NOT NULL,
    roster_batch_no INTEGER NOT NULL,
    PRIMARY KEY (effective_date, cell_key_id)
);

-- (cell, batch) -> persistent ordered-map root.  A root of 0 means "no facts
-- for this cell as of this batch" (for example after a retraction).
CREATE TABLE IF NOT EXISTS roots (
    cell_key_id INTEGER NOT NULL,
    batch_no INTEGER NOT NULL,
    root_id INTEGER NOT NULL,
    PRIMARY KEY (cell_key_id, batch_no)
);

-- Latest root pointer per cell, so an ingest does not sort the cell's roots.
CREATE TABLE IF NOT EXISTS cell_state (
    cell_key_id INTEGER PRIMARY KEY,
    root_id INTEGER NOT NULL,
    batch_no INTEGER NOT NULL
);

-- Immutable persistent treap nodes.  Existing nodes are never updated.
CREATE TABLE IF NOT EXISTS nodes (
    node_id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL,
    fact_json TEXT NOT NULL,
    priority INTEGER NOT NULL,
    left_id INTEGER NOT NULL,
    right_id INTEGER NOT NULL
);
"""


def _normalize_date(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or _DATE_RE.fullmatch(value) is None:
        raise ArchiveError(
            "invalid_effective_date" if field == "effective_date" else "invalid_business_date",
            field=field,
            reason="date must be a calendar date in YYYY-MM-DD form",
        )
    try:
        parsed = _dt.date.fromisoformat(value)
    except ValueError as exc:
        raise ArchiveError(
            "invalid_effective_date" if field == "effective_date" else "invalid_business_date",
            field=field,
            reason="date must be a real calendar date in YYYY-MM-DD form",
        ) from exc
    if parsed.isoformat() != value:
        raise ArchiveError(
            "invalid_effective_date" if field == "effective_date" else "invalid_business_date",
            field=field,
            reason="date must be a real calendar date in YYYY-MM-DD form",
        )
    return value


def _normalize_batch(value: Any, *, field: str, positive: bool = True) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ArchiveError(
            "invalid_batch_no",
            field=field,
            reason="batch number must be an integer",
        )
    if positive and value <= 0:
        raise ArchiveError(
            "invalid_batch_no",
            field=field,
            reason="batch number must be a positive integer",
        )
    return value


def _normalize_id(value: Any, *, field: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ArchiveError(
            "invalid_business_key",
            field=field,
            reason="business row/column identifiers must be strings or integers",
        )
    text = str(value)
    if not text:
        raise ArchiveError(
            "invalid_business_key",
            field=field,
            reason="business row/column identifiers must be non-empty",
        )
    return text


def _priority(key: str) -> int:
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") & _PRIORITY_MASK


def _fact_from_cell(cell: dict[str, Any], *, batch_no: int) -> dict[str, Any]:
    """Whitelist a finalized, privacy-safe staging cell into an archive fact."""

    status = cell.get("status")
    if status not in {"ok", "blank", "undecidable"}:
        raise ArchiveError(
            "bad_roster",
            reason="roster cell has an unknown visibility status",
        )

    if status == "undecidable":
        # Defensive privacy boundary: an undecidable cell has no public text
        # and no word-level evidence.
        text: str | None = None
        reasons = [
            {"code": r.get("code"), "kind": r.get("kind")}
            for r in cell.get("reasons", [])
            if isinstance(r, dict)
        ]
    else:
        text = cell.get("text")
        if not isinstance(text, str):
            raise ArchiveError(
                "bad_roster",
                reason="a judgeable roster cell must carry string text",
            )
        reasons = []

    return {
        "status": status,
        "text": text,
        "reasons": reasons,
        "page_id": None if cell.get("page_id") is None else str(cell["page_id"]),
        "page_no": cell.get("page_no"),
        "cell_id": None if cell.get("cell_id") is None else str(cell["cell_id"]),
        "row_no": cell.get("row_no"),
        "col_no": cell.get("col_no"),
        "roster_batch_no": batch_no,
    }


class ArchiveStore:
    """SQLite-backed persistent archive.

    The class deliberately exposes no method that rewrites an existing treap
    node.  Ingest appends nodes and roots, so a lookup at an old cutoff keeps
    returning the old answer.
    """

    def __init__(self, path: str | os.PathLike[str], *, read_only: bool = False) -> None:
        self.path = os.fspath(path)
        conn: sqlite3.Connection | None = None
        try:
            if read_only:
                uri = Path(self.path).resolve().as_uri() + "?mode=ro"
                conn = sqlite3.connect(uri, uri=True)
                # sqlite3 connects lazily; probe the required schema now so a
                # non-database file fails at open, not mid-query.
                _archive_integrity_check(conn)
                for table in ("batches", "business_cells", "roots", "nodes",
                              "cell_state", "date_cells"):
                    conn.execute("SELECT 1 FROM " + table + " LIMIT 1").fetchall()
            else:
                parent = Path(self.path).expanduser().parent
                parent.mkdir(parents=True, exist_ok=True)
                existed = Path(self.path).exists()
                conn = sqlite3.connect(self.path)
                if existed:
                    # Never initialise our schema over somebody else's file.
                    _archive_integrity_check(conn)
                conn.executescript(_SCHEMA)
                conn.execute(
                    "INSERT INTO archive_meta(key, value) VALUES('format', '1') "
                    "ON CONFLICT(key) DO NOTHING"
                )
                conn.commit()
        except sqlite3.Error as exc:
            # Never echo driver details: they can include a path or file bytes.
            if conn is not None:
                conn.close()
            raise ArchiveError(
                "archive_unavailable",
                reason="archive could not be opened as a database",
                exception_type=type(exc).__name__,
            ) from None
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        # Number of persistent-map node rows read since construction/reset.
        # Preview uses it to prove work is bounded by tree height, not history.
        self.node_reads = 0

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "ArchiveStore":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Persistent treap
    # ------------------------------------------------------------------
    def _node(self, node_id: int) -> sqlite3.Row:
        self.node_reads += 1
        row = self.conn.execute(
            "SELECT node_id, key, fact_json, priority, left_id, right_id "
            "FROM nodes WHERE node_id = ?",
            (node_id,),
        ).fetchone()
        if row is None:
            raise ArchiveError(
                "archive_corrupt",
                reason="persistent map node is missing",
            )
        return row

    def _new_node(
        self,
        key: str,
        fact_json: str,
        priority: int,
        left: int,
        right: int,
    ) -> int:
        cursor = self.conn.execute(
            "INSERT INTO nodes(key, fact_json, priority, left_id, right_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (key, fact_json, priority, left, right),
        )
        return int(cursor.lastrowid)

    def _copy_node(
        self,
        row: sqlite3.Row,
        *,
        fact_json: Any = _UNSET,
        left: Any = _UNSET,
        right: Any = _UNSET,
    ) -> int:
        return self._new_node(
            row["key"],
            row["fact_json"] if fact_json is _UNSET else fact_json,
            int(row["priority"]),
            int(row["left_id"]) if left is _UNSET else int(left),
            int(row["right_id"]) if right is _UNSET else int(right),
        )

    def _split(self, root: int, key: str) -> tuple[int, int]:
        """Persistently split into keys ``< key`` and keys ``> key``.

        An existing node with *key* is dropped from both halves; subsequent
        insertions therefore can never leave duplicate keys behind.
        """

        if root == 0:
            return 0, 0
        node = self._node(root)
        if node["key"] == key:
            return int(node["left_id"]), int(node["right_id"])
        if node["key"] < key:
            left, right = self._split(int(node["right_id"]), key)
            return self._copy_node(node, right=left), right
        left, right = self._split(int(node["left_id"]), key)
        return left, self._copy_node(node, left=right)

    def _merge(self, left: int, right: int) -> int:
        if left == 0:
            return right
        if right == 0:
            return left
        left_node = self._node(left)
        right_node = self._node(right)
        if int(left_node["priority"]) > int(right_node["priority"]):
            return self._copy_node(
                left_node, right=self._merge(int(left_node["right_id"]), right)
            )
        return self._copy_node(
            right_node, left=self._merge(left, int(right_node["left_id"]))
        )

    def _insert(self, root: int, key: str, fact_json: str, priority: int) -> int:
        if root == 0:
            return self._new_node(key, fact_json, priority, 0, 0)
        node = self._node(root)
        if key == node["key"]:
            return self._copy_node(node, fact_json=fact_json)
        if priority > int(node["priority"]):
            left, right = self._split(root, key)
            return self._new_node(key, fact_json, priority, left, right)
        if key < node["key"]:
            return self._copy_node(
                node, left=self._insert(int(node["left_id"]), key, fact_json, priority)
            )
        return self._copy_node(
            node, right=self._insert(int(node["right_id"]), key, fact_json, priority)
        )

    def _delete(self, root: int, key: str) -> int:
        if root == 0:
            return 0
        node = self._node(root)
        if key < node["key"]:
            return self._copy_node(
                node, left=self._delete(int(node["left_id"]), key)
            )
        if key > node["key"]:
            return self._copy_node(
                node, right=self._delete(int(node["right_id"]), key)
            )
        return self._merge(int(node["left_id"]), int(node["right_id"]))

    def _predecessor(self, root: int, bound: str) -> sqlite3.Row | None:
        """Newest key ``<= bound``; one descent, no history scan."""

        best: sqlite3.Row | None = None
        node_id = root
        while node_id != 0:
            node = self._node(node_id)
            if node["key"] <= bound:
                best = node
                node_id = int(node["right_id"])
            else:
                node_id = int(node["left_id"])
        return best

    def _predecessor_strict(self, root: int, bound: str) -> sqlite3.Row | None:
        """Newest key ``< bound``; one descent."""

        best: sqlite3.Row | None = None
        node_id = root
        while node_id != 0:
            node = self._node(node_id)
            if node["key"] < bound:
                best = node
                node_id = int(node["right_id"])
            else:
                node_id = int(node["left_id"])
        return best

    def _successor_strict(self, root: int, bound: str) -> sqlite3.Row | None:
        """Smallest key ``> bound``; one descent.  This is where an interval
        opened at *bound* closes for this cell."""

        best: sqlite3.Row | None = None
        node_id = root
        while node_id != 0:
            node = self._node(node_id)
            if node["key"] > bound:
                best = node
                node_id = int(node["left_id"])
            else:
                node_id = int(node["right_id"])
        return best

    # ------------------------------------------------------------------
    # Business-cell helpers
    # ------------------------------------------------------------------
    def _cell_key_id(self, row_id: str, col_id: str) -> int | None:
        row = self.conn.execute(
            "SELECT cell_key_id FROM business_cells "
            "WHERE row_id = ? AND col_id = ?",
            (row_id, col_id),
        ).fetchone()
        return None if row is None else int(row["cell_key_id"])

    def _get_or_create_cell_key_id(self, row_id: str, col_id: str) -> int:
        self.conn.execute(
            "INSERT INTO business_cells(row_id, col_id) VALUES (?, ?) "
            "ON CONFLICT(row_id, col_id) DO NOTHING",
            (row_id, col_id),
        )
        return self._cell_key_id(row_id, col_id)  # type: ignore[return-value]

    def _current_root(self, cell_key_id: int) -> int:
        row = self.conn.execute(
            "SELECT root_id FROM cell_state WHERE cell_key_id = ?",
            (cell_key_id,),
        ).fetchone()
        return 0 if row is None else int(row["root_id"])

    def _record_root(self, cell_key_id: int, batch_no: int, root_id: int) -> None:
        self.conn.execute(
            "INSERT INTO roots(cell_key_id, batch_no, root_id) VALUES (?, ?, ?)",
            (cell_key_id, batch_no, root_id),
        )
        self.conn.execute(
            "INSERT INTO cell_state(cell_key_id, root_id, batch_no) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(cell_key_id) DO UPDATE SET "
            "root_id = excluded.root_id, batch_no = excluded.batch_no",
            (cell_key_id, root_id, batch_no),
        )

    def _put_date_fact(
        self,
        cell_key_id: int,
        effective_date: str,
        fact: dict[str, Any],
        batch_no: int,
    ) -> None:
        fact_json = json.dumps(
            fact, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )
        root = self._current_root(cell_key_id)
        try:
            new_root = self._insert(
                root, effective_date, fact_json, _priority(effective_date)
            )
        except RecursionError:
            raise ArchiveError(
                "archive_too_deep",
                effective_date=effective_date,
                reason="persistent map depth exceeded the safe limit",
            ) from None
        self._record_root(cell_key_id, batch_no, new_root)
        self.conn.execute(
            "INSERT INTO date_cells(effective_date, cell_key_id, roster_batch_no) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(effective_date, cell_key_id) "
            "DO UPDATE SET roster_batch_no = excluded.roster_batch_no",
            (effective_date, cell_key_id, batch_no),
        )

    def _delete_date_fact(
        self, cell_key_id: int, effective_date: str, batch_no: int
    ) -> None:
        root = self._current_root(cell_key_id)
        try:
            new_root = self._delete(root, effective_date)
        except RecursionError:
            raise ArchiveError(
                "archive_too_deep",
                effective_date=effective_date,
                reason="persistent map depth exceeded the safe limit",
            ) from None
        self._record_root(cell_key_id, batch_no, new_root)
        self.conn.execute(
            "DELETE FROM date_cells WHERE effective_date = ? AND cell_key_id = ?",
            (effective_date, cell_key_id),
        )

    def _validate_next_batch(self, batch_no: int) -> None:
        row = self.conn.execute("SELECT MAX(batch_no) AS last FROM batches").fetchone()
        last = 0 if row is None or row["last"] is None else int(row["last"])
        if batch_no <= last:
            raise ArchiveError(
                "batch_not_increasing",
                supplied_batch_no=batch_no,
                last_batch_no=last,
                reason="batch numbers must increase with every accepted ingest",
            )

    def _previous_date_cells(self, effective_date: str) -> dict[int, int]:
        rows = self.conn.execute(
            "SELECT cell_key_id, roster_batch_no FROM date_cells "
            "WHERE effective_date = ?",
            (effective_date,),
        ).fetchall()
        return {
            int(r["cell_key_id"]): int(r["roster_batch_no"]) for r in rows
        }

    # ------------------------------------------------------------------
    # Ingest / query
    # ------------------------------------------------------------------
    def submit(
        self,
        *,
        batch_no: int,
        effective_date: str,
        staging: StagingStore,
    ) -> dict[str, Any]:
        """Atomically install a staged roster as a wholesale replacement."""

        self.conn.execute("BEGIN IMMEDIATE")
        committed = False
        try:
            self._validate_next_batch(batch_no)
            previous = self._previous_date_cells(effective_date)
            removed = set(previous)

            cell_count = 0
            inserted = False
            for cell in staging.iter_cell_rows():
                row_id = cell["row_id"]
                col_id = cell["col_id"]
                cell_key_id = self._get_or_create_cell_key_id(row_id, col_id)
                removed.discard(cell_key_id)
                fact = _fact_from_cell(cell, batch_no=batch_no)
                self._put_date_fact(cell_key_id, effective_date, fact, batch_no)
                inserted = True
                cell_count += 1

            if not inserted and not previous:
                raise ArchiveError(
                    "empty_roster",
                    effective_date=effective_date,
                    reason="a submitted roster declared no business cells",
                )

            for cell_key_id in removed:
                self._delete_date_fact(cell_key_id, effective_date, batch_no)

            self.conn.execute(
                "INSERT INTO batches(batch_no, effective_date, action, cell_count) "
                "VALUES (?, ?, 'submit', ?)",
                (batch_no, effective_date, cell_count),
            )
            self.conn.execute("COMMIT")
            committed = True
            return {
                "action": "submit",
                "batch_no": batch_no,
                "effective_date": effective_date,
                "cell_count": cell_count,
                "removed_cell_count": len(removed),
            }
        except BaseException:
            if not committed:
                self.conn.execute("ROLLBACK")
            raise

    def retract(self, *, batch_no: int, effective_date: str) -> dict[str, Any]:
        """Atomically remove the date's currently effective roster."""

        self.conn.execute("BEGIN IMMEDIATE")
        committed = False
        try:
            self._validate_next_batch(batch_no)
            previous = self._previous_date_cells(effective_date)
            if not previous:
                raise ArchiveError(
                    "no_roster_to_retract",
                    effective_date=effective_date,
                    reason="this effective date has no currently valid roster",
                )
            for cell_key_id in previous:
                self._delete_date_fact(cell_key_id, effective_date, batch_no)
            self.conn.execute(
                "DELETE FROM date_cells WHERE effective_date = ?",
                (effective_date,),
            )
            self.conn.execute(
                "INSERT INTO batches(batch_no, effective_date, action, cell_count) "
                "VALUES (?, ?, 'retract', ?)",
                (batch_no, effective_date, len(previous)),
            )
            self.conn.execute("COMMIT")
            committed = True
            return {
                "action": "retract",
                "batch_no": batch_no,
                "effective_date": effective_date,
                "removed_cell_count": len(previous),
            }
        except BaseException:
            if not committed:
                self.conn.execute("ROLLBACK")
            raise

    def query(
        self,
        *,
        cutoff_batch_no: int,
        business_date: str,
        row_id: str,
        col_id: str,
    ) -> dict[str, Any]:
        """One indexed root lookup plus one treap predecessor descent."""

        base = {
            "record_type": "archive_cell",
            "cutoff_batch_no": cutoff_batch_no,
            "business_date": business_date,
            "row_id": row_id,
            "col_id": col_id,
        }
        cell_key_id = self._cell_key_id(row_id, col_id)
        if cell_key_id is None:
            return {**base, "found": False, "result": "no_record"}

        root_row = self.conn.execute(
            "SELECT root_id FROM roots WHERE cell_key_id = ? AND batch_no <= ? "
            "ORDER BY batch_no DESC LIMIT 1",
            (cell_key_id, cutoff_batch_no),
        ).fetchone()
        if root_row is None or int(root_row["root_id"]) == 0:
            return {**base, "found": False, "result": "no_record"}

        node = self._predecessor(int(root_row["root_id"]), business_date)
        if node is None:
            return {**base, "found": False, "result": "no_record"}

        fact = json.loads(node["fact_json"])
        out = {
            **base,
            "found": True,
            "status": fact["status"],
            "text": fact.get("text"),
            "effective_date": node["key"],
            "ingest_batch_no": fact["roster_batch_no"],
            "page_id": fact.get("page_id"),
            "page_no": fact.get("page_no"),
            "cell_id": fact.get("cell_id"),
            "row_no": fact.get("row_no"),
            "col_no": fact.get("col_no"),
        }
        if fact["status"] == "undecidable":
            out["reasons"] = fact.get("reasons", [])
        return out

    def list_batches(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT batch_no, effective_date, action, cell_count "
            "FROM batches ORDER BY batch_no"
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Read-only impact preview
    # ------------------------------------------------------------------
    def preview_operation(
        self,
        *,
        batch_no: int,
        effective_date: str,
        action: str,
        staging: StagingStore | None = None,
    ) -> list[dict[str, Any]]:
        """Simulate one submit/retract without touching the archive.

        Work is bounded by the operation itself: the new roster's cells plus
        the cells currently registered for *effective_date*.  For each such
        cell we perform three treap descents (predecessor ``<= D``,
        predecessor ``< D`` and successor ``> D``); we never enumerate
        calendar days and never replay historical batches.

        The returned records are already in deterministic order.
        """

        self.node_reads = 0
        # Mirror ingest's acceptance checks; all are read-only SELECTs.
        self._validate_next_batch(batch_no)

        previous = self._date_cell_rows(effective_date)
        records: list[dict[str, Any]] = []

        if action == "submit":
            if staging is None:
                raise ArchiveError(
                    "missing_roster",
                    reason="submit requires a roster page description",
                )
            new_facts: list[tuple[str, str, dict[str, Any]]] = []
            new_pairs: set[tuple[str, str]] = set()
            for cell in staging.iter_cell_rows():
                row_id = cell["row_id"]
                col_id = cell["col_id"]
                fact = _fact_from_cell(cell, batch_no=batch_no)
                new_facts.append((row_id, col_id, fact))
                new_pairs.add((row_id, col_id))

            if not new_facts and not previous:
                raise ArchiveError(
                    "empty_roster",
                    effective_date=effective_date,
                    reason="a submitted roster declared no business cells",
                )

            for row_id, col_id, fact in new_facts:
                record = self._preview_one_cell(
                    cell_key_id=self._cell_key_id(row_id, col_id),
                    row_id=row_id,
                    col_id=col_id,
                    effective_date=effective_date,
                    batch_no=batch_no,
                    action=action,
                    new_fact=fact,
                )
                if record is not None:
                    records.append(record)

            for cell_key_id, row_id, col_id in previous:
                if (row_id, col_id) in new_pairs:
                    continue
                record = self._preview_one_cell(
                    cell_key_id=cell_key_id,
                    row_id=row_id,
                    col_id=col_id,
                    effective_date=effective_date,
                    batch_no=batch_no,
                    action=action,
                    new_fact=None,
                )
                if record is not None:
                    records.append(record)
        else:  # retract
            if not previous:
                raise ArchiveError(
                    "no_roster_to_retract",
                    effective_date=effective_date,
                    reason="this effective date has no currently valid roster",
                )
            for cell_key_id, row_id, col_id in previous:
                record = self._preview_one_cell(
                    cell_key_id=cell_key_id,
                    row_id=row_id,
                    col_id=col_id,
                    effective_date=effective_date,
                    batch_no=batch_no,
                    action=action,
                    new_fact=None,
                )
                if record is not None:
                    records.append(record)

        records.sort(
            key=lambda r: (
                r["row_id"],
                r["col_id"],
                r["interval"]["start"],
                r["interval"]["end"] or "",
            )
        )
        return records

    def _date_cell_rows(
        self, effective_date: str
    ) -> list[tuple[int, str, str]]:
        rows = self.conn.execute(
            "SELECT dc.cell_key_id AS cell_key_id, bc.row_id AS row_id, "
            "bc.col_id AS col_id FROM date_cells AS dc "
            "JOIN business_cells AS bc ON bc.cell_key_id = dc.cell_key_id "
            "WHERE dc.effective_date = ? ORDER BY dc.cell_key_id",
            (effective_date,),
        ).fetchall()
        return [
            (int(r["cell_key_id"]), r["row_id"], r["col_id"]) for r in rows
        ]

    def _preview_one_cell(
        self,
        *,
        cell_key_id: int | None,
        row_id: str,
        col_id: str,
        effective_date: str,
        batch_no: int,
        action: str,
        new_fact: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        """Diff one cell around *effective_date*.

        Only dates in ``[D, next date after D)`` can change, because the
        operation rewrites exactly date ``D`` and dates after ``D`` are the
        same in both maps.  Admitted cells therefore need one interval, not a
        per-day walk.
        """

        old_root = 0 if cell_key_id is None else self._current_root(cell_key_id)

        old_node = self._predecessor(old_root, effective_date)
        before = (
            _answer_from_node(old_node)
            if old_node is not None
            else _no_record_answer()
        )

        if new_fact is not None:
            after = _answer_from_fact(new_fact, effective_date)
        else:
            previous_node = self._predecessor_strict(old_root, effective_date)
            after = (
                _answer_from_node(previous_node)
                if previous_node is not None
                else _no_record_answer()
            )

        if before == after:
            return None

        successor = self._successor_strict(old_root, effective_date)
        end = None if successor is None else successor["key"]

        return {
            "record_type": "archive_impact",
            "action": action,
            "batch_no": batch_no,
            "effective_date": effective_date,
            "row_id": row_id,
            "col_id": col_id,
            "interval": {"start": effective_date, "end": end},
            "before": before,
            "after": after,
        }


def _no_record_answer() -> dict[str, Any]:
    return {"found": False, "result": "no_record"}


def _answer_from_fact(
    fact: dict[str, Any], effective_date: str
) -> dict[str, Any]:
    """Shape a stored fact like one single-cell query answer (no evidence)."""

    out: dict[str, Any] = {
        "found": True,
        "status": fact["status"],
        "text": fact.get("text"),
        "effective_date": effective_date,
        "ingest_batch_no": fact["roster_batch_no"],
        "page_id": fact.get("page_id"),
        "page_no": fact.get("page_no"),
        "cell_id": fact.get("cell_id"),
        "row_no": fact.get("row_no"),
        "col_no": fact.get("col_no"),
    }
    if fact["status"] == "undecidable":
        out["reasons"] = fact.get("reasons", [])
    return out


def _answer_from_node(node: sqlite3.Row) -> dict[str, Any]:
    return _answer_from_fact(json.loads(node["fact_json"]), node["key"])


# ----------------------------------------------------------------------
# Module-level entry points
# ----------------------------------------------------------------------
def _stage_roster(roster_path: str | os.PathLike[str]) -> StagingStore:
    fd, name = tempfile.mkstemp(prefix="roster-archive-", suffix=".sqlite3")
    os.close(fd)
    staging = StagingStore(name)
    try:
        # The existing streaming reconstructor validates the page description
        # and records only privacy-safe, finalized cells.
        for _ in process_jsonl(roster_path, staging):
            pass
    except BaseException:
        staging.destroy()
        raise
    return staging


def _archive_integrity_check(conn: sqlite3.Connection) -> None:
    """Refuse to write into a file that is not one of our archives."""

    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    if "archive_meta" not in tables:
        raise ArchiveError(
            "not_an_archive",
            reason="existing database is not a roster archive",
        )
    row = conn.execute(
        "SELECT value FROM archive_meta WHERE key = 'format'"
    ).fetchone()
    if row is None or row[0] != "1":
        raise ArchiveError(
            "not_an_archive",
            reason="existing database is not a roster archive",
        )


def ingest(
    archive_path: str | os.PathLike[str],
    *,
    batch_no: int,
    effective_date: str,
    action: str = "submit",
    roster_path: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Submit or retract one roster.  Failures leave archive content unchanged."""

    if action not in {"submit", "retract"}:
        raise ArchiveError(
            "invalid_action", reason="action must be 'submit' or 'retract'"
        )
    batch = _normalize_batch(batch_no, field="batch_no")
    date = _normalize_date(effective_date, field="effective_date")

    if action == "submit":
        if roster_path is None:
            raise ArchiveError(
                "missing_roster", reason="submit requires a roster page description"
            )
        staging = _stage_roster(roster_path)
        try:
            # Validate the existing file (and its batch order) read-only before
            # opening it for writing: a rejected ingest must not create or
            # modify the archive.
            if Path(archive_path).exists():
                probe = ArchiveStore(archive_path, read_only=True)
                try:
                    probe._validate_next_batch(batch)
                finally:
                    probe.close()
            store = ArchiveStore(archive_path)
            try:
                result = store.submit(
                    batch_no=batch, effective_date=date, staging=staging
                )
            finally:
                store.close()
        finally:
            staging.destroy()
        result["status"] = "ok"
        result["archive"] = os.fspath(archive_path)
        return result

    if roster_path is not None:
        raise ArchiveError(
            "unexpected_roster", reason="retract does not take a roster"
        )
    if not Path(archive_path).exists():
        raise ArchiveError(
            "no_roster_to_retract",
            effective_date=date,
            reason="this effective date has no currently valid roster",
        )
    # Validate the existing file before opening it for writing.
    probe = ArchiveStore(archive_path, read_only=True)
    try:
        probe._validate_next_batch(batch)
    finally:
        probe.close()
    store = ArchiveStore(archive_path)
    try:
        result = store.retract(batch_no=batch, effective_date=date)
    finally:
        store.close()
    result["status"] = "ok"
    result["archive"] = os.fspath(archive_path)
    return result


def query(
    archive_path: str | os.PathLike[str],
    *,
    cutoff_batch_no: int,
    business_date: str,
    row_id: Any,
    col_id: Any,
) -> dict[str, Any]:
    """Look up one business cell as of a batch cutoff."""

    cutoff = _normalize_batch(cutoff_batch_no, field="cutoff_batch_no", positive=False)
    if cutoff < 0:
        raise ArchiveError(
            "invalid_batch_no",
            field="cutoff_batch_no",
            reason="cutoff batch number must be a non-negative integer",
        )
    date = _normalize_date(business_date, field="business_date")
    row = _normalize_id(row_id, field="row_id")
    col = _normalize_id(col_id, field="col_id")

    path = Path(archive_path)
    if not path.exists():
        raise ArchiveError(
            "archive_not_found", reason="archive file does not exist"
        )
    store = ArchiveStore(archive_path, read_only=True)
    try:
        return store.query(
            cutoff_batch_no=cutoff,
            business_date=date,
            row_id=row,
            col_id=col,
        )
    finally:
        store.close()


def list_batches(archive_path: str | os.PathLike[str]) -> dict[str, Any]:
    path = Path(archive_path)
    if not path.exists():
        raise ArchiveError(
            "archive_not_found", reason="archive file does not exist"
        )
    store = ArchiveStore(archive_path, read_only=True)
    try:
        return {
            "status": "ok",
            "archive": os.fspath(archive_path),
            "batches": store.list_batches(),
        }
    finally:
        store.close()


def preview(
    archive_path: str | os.PathLike[str],
    *,
    batch_no: int,
    effective_date: str,
    action: str = "submit",
    roster_path: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Read-only preview of what one ingest would change, as JSONL diffs.

    The roster (for ``submit``) is validated by the same reconstruction path
    the real ingest uses.  The archive is opened read-only; no batch is added,
    no persisted node is written, and no file is created if it was absent.
    On any error nothing is emitted and the archive stays byte-for-byte intact.
    """

    if action not in {"submit", "retract"}:
        raise ArchiveError(
            "invalid_action", reason="action must be 'submit' or 'retract'"
        )
    batch = _normalize_batch(batch_no, field="batch_no")
    date = _normalize_date(effective_date, field="effective_date")

    if action == "submit" and roster_path is None:
        raise ArchiveError(
            "missing_roster", reason="submit requires a roster page description"
        )
    if action == "retract" and roster_path is not None:
        raise ArchiveError(
            "unexpected_roster", reason="retract does not take a roster"
        )

    if not Path(archive_path).exists():
        raise ArchiveError(
            "archive_not_found", reason="archive file does not exist"
        )

    staging: StagingStore | None = None
    try:
        if action == "submit":
            # Same validator and privacy whitelist as the real ingest.
            staging = _stage_roster(roster_path)  # type: ignore[arg-type]

        store = ArchiveStore(archive_path, read_only=True)
        try:
            records = store.preview_operation(
                batch_no=batch,
                effective_date=date,
                action=action,
                staging=staging,
            )
            node_reads = store.node_reads
        finally:
            store.close()
    finally:
        if staging is not None:
            staging.destroy()

    return {
        "status": "ok",
        "archive": os.fspath(archive_path),
        "action": action,
        "batch_no": batch,
        "effective_date": date,
        "changed_cell_count": len(records),
        "records": records,
        "node_reads": node_reads,
    }

