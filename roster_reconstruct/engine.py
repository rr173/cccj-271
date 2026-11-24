"""Core page reconstruction engine.

The public entry point is :class:`PageBuilder`.  It validates one page, uses a
spatial index to find later overlapping primitives, and subtracts their exact
integer rectangles from each word box.  Only fully visible words wholly
contained in one cell become text/evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Hashable

from .errors import PageDescriptionError
from .geometry import Rect, as_rect, contains, ensure_inside, subtract_rect
from .index import BoxIndex

PARTIAL_REASON = "partial_occlusion_visible_area"
CROSS_BOUNDARY_REASON = "fully_visible_word_not_contained_in_one_cell"

_REASON_MESSAGES = {
    PARTIAL_REASON: (
        "a partially occluded word has visible area intersecting this cell"
    ),
    CROSS_BOUNDARY_REASON: (
        "a fully visible word is not wholly contained in this and every "
        "positively intersected cell"
    ),
}


@dataclass(slots=True)
class Primitive:
    seq: int
    kind: str
    rect: Rect
    text: str | None = None
    word_id: Hashable | None = None
    supplied_order: int | None = None


def _optional_int(value: Any, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise PageDescriptionError(
            "invalid_metadata",
            field=field,
            reason=f"{field} must be an integer or null",
        )
    return value


def _id(value: Any, field: str, *, required: bool = True) -> str | None:
    if value is None:
        if required:
            raise PageDescriptionError(
                "invalid_metadata", field=field, reason=f"{field} is required"
            )
        return None
    if isinstance(value, bool):
        raise PageDescriptionError(
            "invalid_metadata", field=field, reason=f"{field} must be a string"
        )
    text = str(value)
    if not text:
        raise PageDescriptionError(
            "invalid_metadata", field=field, reason=f"{field} must be non-empty"
        )
    return text


def _pick(record: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in record and record[key] is not None:
            return record[key]
    return None


def _word_ref(value: Any) -> Hashable | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise PageDescriptionError(
            "invalid_word_id",
            reason="word_id/word_no must be a string or integer",
        )
    if isinstance(value, str) and not value:
        raise PageDescriptionError(
            "invalid_word_id", reason="word_id must be non-empty"
        )
    return value


class PageBuilder:
    """Incrementally build and reconstruct one page."""

    def __init__(
        self,
        bounds: Any,
        *,
        page_id: str | None = None,
        page_no: int | None = None,
    ) -> None:
        self.page_id = page_id
        self.page_no = _optional_int(page_no, "page_no") if page_no is not None else None
        self.bounds = self._bounds(bounds)
        self.cells: list[dict[str, Any]] = []
        self.cell_rects: dict[str, Rect] = {}
        self._cell_ids: set[str] = set()
        self._row_col: set[tuple[str, str]] = set()
        self.prims: list[Primitive] = []

    @staticmethod
    def _bounds(value: Any) -> Rect:
        if isinstance(value, dict):
            if "width" in value or "height" in value:
                width = value.get("width")
                height = value.get("height")
                if (
                    isinstance(width, int)
                    and not isinstance(width, bool)
                    and isinstance(height, int)
                    and not isinstance(height, bool)
                ):
                    value = [
                        value.get("x0", 0),
                        value.get("y0", 0),
                        width,
                        height,
                    ]
            else:
                value = value.get("rect") or value.get("bounds")
        return as_rect(value, what="page_bounds")

    # ------------------------------------------------------------------
    # Declaration and draw-order ingestion
    # ------------------------------------------------------------------
    def add_cell(
        self,
        rect: Any,
        *,
        cell_id: Any = None,
        row_id: Any = None,
        col_id: Any = None,
        row_no: Any = None,
        col_no: Any = None,
        reading_order: Any = None,
        **extra: Any,
    ) -> None:
        rid = _id(row_id, "row_id")
        cid_col = _id(col_id, "col_id")
        assert rid is not None and cid_col is not None
        if cell_id is None:
            cid = f"{self.page_id or 'page'}:{rid}:{cid_col}"
        else:
            cid = _id(cell_id, "cell_id")
        assert cid is not None

        if cid in self._cell_ids:
            raise PageDescriptionError(
                "duplicate_cell",
                page_id=self.page_id,
                cell_id=cid,
            )
        pair = (rid, cid_col)
        if pair in self._row_col:
            raise PageDescriptionError(
                "duplicate_logical_unit",
                page_id=self.page_id,
                row_id=rid,
                col_id=cid_col,
            )

        r = as_rect(rect, what=f"cell[{cid}].rect")
        ensure_inside(self.bounds, r, what=f"cell[{cid}]")

        cell = {
            "cell_id": cid,
            "row_id": rid,
            "col_id": cid_col,
            "row_no": _optional_int(row_no, "row_no"),
            "col_no": _optional_int(col_no, "col_no"),
            "reading_order": _optional_int(reading_order, "reading_order"),
            "rect": r,
            "input_index": len(self.cells),
        }
        if extra:
            # Preserve a small, whitelisted extension point without allowing
            # callers to alter geometry semantics.
            cell["extra"] = extra
        self.cells.append(cell)
        self.cell_rects[cid] = r
        self._cell_ids.add(cid)
        self._row_col.add(pair)

    def add_word(
        self,
        rect: Any,
        text: Any,
        *,
        word_id: Any = None,
        draw_order: Any = None,
        **_extra: Any,
    ) -> int:
        if not isinstance(text, str):
            raise PageDescriptionError(
                "invalid_word_text",
                reason="word text must be a JSON string",
            )
        if text == "":
            raise PageDescriptionError(
                "invalid_word_text",
                reason="an opaque word primitive must contain non-empty text",
            )

        r = as_rect(rect, what="word.rect")
        ensure_inside(self.bounds, r, what="word")

        seq = len(self.prims)
        supplied = _optional_int(draw_order, "draw_order")
        if supplied is not None and supplied != seq + 1:
            raise PageDescriptionError(
                "bad_draw_order",
                expected=seq + 1,
                actual=supplied,
                reason="draw_order must equal the one-based primitive position",
            )

        wid = _word_ref(word_id) if word_id is not None else None

        self.prims.append(
            Primitive(
                seq=seq,
                kind="word",
                rect=r,
                text=text,
                word_id=wid,
                supplied_order=supplied,
            )
        )
        return seq

    def add_blank(
        self,
        rect: Any,
        *,
        draw_order: Any = None,
        **_extra: Any,
    ) -> int:
        r = as_rect(rect, what="blank.rect")
        ensure_inside(self.bounds, r, what="blank")
        seq = len(self.prims)
        supplied = _optional_int(draw_order, "draw_order")
        if supplied is not None and supplied != seq + 1:
            raise PageDescriptionError(
                "bad_draw_order",
                expected=seq + 1,
                actual=supplied,
            )
        self.prims.append(
            Primitive(seq=seq, kind="blank", rect=r, supplied_order=supplied)
        )
        return seq

    def add_record(self, record: dict[str, Any]) -> None:
        """Dispatch a canonical page-scoped record."""

        kind = _pick(record, "type", "kind")
        if kind == "cell":
            row_value = _pick(record, "row_id", "row_key", "row")
            col_value = _pick(
                record, "col_id", "column_id", "col_key", "col", "column"
            )
            row_no = _pick(record, "row_no", "row_index")
            col_no = _pick(record, "col_no", "col_index")
            if row_no is None and isinstance(row_value, int) and not isinstance(
                row_value, bool
            ):
                row_no = row_value
            if col_no is None and isinstance(col_value, int) and not isinstance(
                col_value, bool
            ):
                col_no = col_value
            self.add_cell(
                _pick(record, "rect", "bbox"),
                cell_id=_pick(record, "cell_id", "id", "unit_id"),
                row_id=row_value,
                col_id=col_value,
                row_no=row_no,
                col_no=col_no,
                reading_order=_pick(record, "reading_order", "order"),
            )
        elif kind in {"word", "text_word"}:
            self.add_word(
                _pick(record, "rect", "bbox"),
                _pick(record, "text", "value", "content"),
                word_id=_pick(record, "word_id", "id", "word_no"),
                draw_order=_pick(record, "draw_order", "order"),
            )
        elif kind in {"blank", "blank_box", "white_box"}:
            self.add_blank(
                _pick(record, "rect", "bbox"),
                draw_order=_pick(record, "draw_order", "order"),
            )
        else:
            raise PageDescriptionError(
                "unsupported_layer_primitive",
                primitive_type=str(kind) if kind is not None else None,
                reason="only opaque word and opaque blank primitives are allowed",
            )

    # ------------------------------------------------------------------
    # Reconstruction
    # ------------------------------------------------------------------
    def _validate_cell_geometry(self) -> BoxIndex:
        items = [(c["cell_id"], c["rect"]) for c in self.cells]
        index = BoxIndex(items)
        for cell in self.cells:
            cid = cell["cell_id"]
            other = index.first_overlap(cell["rect"], ignore_id=cid)
            if other is not None:
                raise PageDescriptionError(
                    "overlapping_cells",
                    page_id=self.page_id,
                    cell_id=cid,
                    other_cell_id=other,
                    reason="declared rectangle units have positive-area overlap",
                )
        return index

    def finish(self) -> dict[str, Any]:
        """Compute the safe page result and release source word texts."""

        cell_index = self._validate_cell_geometry()
        # Static spatial index over all primitives.  Draw order is applied by
        # filtering sequence numbers after an indexed geometry query.
        primitive_index = BoxIndex([(p.seq, p.rect) for p in self.prims])

        assigned: dict[str, list[dict[str, Any]]] = {
            c["cell_id"]: [] for c in self.cells
        }
        invalid: dict[str, set[str]] = {}

        counts = {
            "primitive_count": len(self.prims),
            "word_count": sum(p.kind == "word" for p in self.prims),
            "blank_count": sum(p.kind == "blank" for p in self.prims),
            "fully_visible_word_count": 0,
            "partially_occluded_word_count": 0,
            "fully_hidden_word_count": 0,
        }
        visible_word_without_cell = 0

        def invalidate(cell_id: str, code: str) -> None:
            if cell_id not in invalid:
                assigned[cell_id].clear()
                invalid[cell_id] = {code}
            else:
                invalid[cell_id].add(code)

        for prim in self.prims:
            if prim.kind == "blank":
                continue

            assert prim.text is not None
            fragments: list[Rect] = [prim.rect]
            has_later_overlap = False

            for other_seq in primitive_index.query(prim.rect):
                if other_seq <= prim.seq:
                    continue
                has_later_overlap = True
                fragments = subtract_rect(fragments, self.prims[other_seq].rect)
                if not fragments:
                    break

            if not has_later_overlap:
                counts["fully_visible_word_count"] += 1
                candidates = list(cell_index.query(prim.rect))
                contained = [
                    cid
                    for cid in candidates
                    if contains(self.cell_rects[cid], prim.rect)
                ]
                if len(candidates) == 1 and len(contained) == 1:
                    cid = candidates[0]
                    if cid not in invalid:
                        x0, y0, _x1, _y1 = prim.rect
                        assigned[cid].append(
                            {
                                "seq": prim.seq,
                                "top": y0,
                                "left": x0,
                                "text": prim.text,
                                "word_id": prim.word_id,
                                "rect": prim.rect,
                            }
                        )
                else:
                    if not candidates:
                        visible_word_without_cell += 1
                    for cid in candidates:
                        invalidate(cid, CROSS_BOUNDARY_REASON)
            elif not fragments:
                counts["fully_hidden_word_count"] += 1
            else:
                counts["partially_occluded_word_count"] += 1
                for fragment in fragments:
                    for cid in cell_index.query(fragment):
                        invalidate(cid, PARTIAL_REASON)

        cell_results: list[dict[str, Any]] = []
        for cell in self.cells:
            cid = cell["cell_id"]
            base = {
                "record_type": "cell_result",
                "page_id": self.page_id,
                "page_no": self.page_no,
                "cell_id": cid,
                "row_id": cell["row_id"],
                "col_id": cell["col_id"],
                "row_no": cell["row_no"],
                "col_no": cell["col_no"],
                "reading_order": cell["reading_order"],
                "rect": list(cell["rect"]),
            }

            reasons = invalid.get(cid)
            if reasons:
                base["status"] = "undecidable"
                base["text"] = None
                base["reasons"] = [
                    {"code": code, "kind": "geometry", "message": _REASON_MESSAGES[code]}
                    for code in sorted(reasons)
                ]
                base["evidence"] = []
                cell_results.append(base)
                continue

            words = assigned[cid]
            words.sort(key=lambda w: (w["top"], w["left"], w["seq"]))
            text = " ".join(w["text"] for w in words)
            evidence = [
                {
                    "word_id": w["word_id"],
                    "draw_order": w["seq"] + 1,
                    "rect": list(w["rect"]),
                }
                for w in words
            ]
            base["status"] = "ok" if words else "blank"
            base["text"] = text
            base["evidence"] = evidence
            cell_results.append(base)

        undecidable = sum(1 for c in cell_results if c["status"] == "undecidable")
        counts["cell_count"] = len(cell_results)
        counts["undecidable_cell_count"] = undecidable

        diagnostics: list[dict[str, Any]] = []
        if visible_word_without_cell:
            diagnostics.append(
                {
                    "code": "visible_word_without_declared_cell",
                    "kind": "geometry",
                    "count": visible_word_without_cell,
                    "message": (
                        "fully visible words had no positive-area declared cell"
                    ),
                }
            )

        # Do not retain source word strings on the builder after finish.
        self.prims.clear()

        return {
            "record_type": "page_result",
            "page_id": self.page_id,
            "page_no": self.page_no,
            "bounds": list(self.bounds),
            "cells": cell_results,
            "diagnostics": diagnostics,
            "stats": counts,
        }
