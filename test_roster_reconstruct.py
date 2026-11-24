from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from roster_reconstruct.book import (  # noqa: E402
    iter_business_rows,
    process_jsonl,
    process_page_description,
)
from roster_reconstruct.engine import PageBuilder  # noqa: E402
from roster_reconstruct.errors import PageDescriptionError  # noqa: E402
from roster_reconstruct.export import (  # noqa: E402
    write_business_rows_csv,
    write_cells_jsonl,
    write_rows_jsonl,
)
from roster_reconstruct.fixtures import (  # noqa: E402
    CANONICAL_EXPECTED,
    PRIVACY_SECRETS,
    write_bad_fixtures,
    write_canonical_book,
    write_large_book,
)
from roster_reconstruct.geometry import intersection, subtract_rect  # noqa: E402
from roster_reconstruct.index import BoxIndex  # noqa: E402
from roster_reconstruct.staging import StagingStore  # noqa: E402


def cell(cell_id: str, row: str, col: str, rect: list[int]) -> dict:
    return {
        "cell_id": cell_id,
        "row_id": row,
        "col_id": col,
        "row_no": 1,
        "col_no": 1,
        "rect": rect,
    }


def word(text: str, rect: list[int], word_id: str | None = None) -> dict:
    d: dict = {"type": "word", "text": text, "rect": rect}
    if word_id is not None:
        d["word_id"] = word_id
    return d


def blank(rect: list[int]) -> dict:
    return {"type": "blank", "rect": rect}


def cells_by_id(result: dict) -> dict:
    return {c["cell_id"]: c for c in result["cells"]}


class GeometryTests(unittest.TestCase):
    def test_half_open_boundary_contact_is_not_intersection(self) -> None:
        self.assertIsNone(intersection((0, 0, 10, 10), (10, 0, 20, 10)))
        self.assertIsNone(intersection((0, 0, 10, 10), (0, 10, 10, 20)))
        self.assertEqual(
            intersection((0, 0, 11, 10), (10, 0, 20, 10)), (10, 0, 11, 10)
        )

    def test_subtract_rect_partitions_remainder(self) -> None:
        # Cutter removes the middle vertical band.
        fragments = subtract_rect([(0, 0, 100, 100)], (40, 20, 60, 80))
        self.assertIn((0, 0, 40, 100), fragments)
        self.assertIn((60, 0, 100, 100), fragments)
        self.assertIn((40, 0, 60, 20), fragments)
        self.assertIn((40, 80, 60, 100), fragments)

        # Boundary-only cutter changes nothing.
        original = [(0, 0, 10, 10)]
        self.assertEqual(subtract_rect(original, (10, 0, 20, 10)), original)

        # Full coverage leaves nothing.
        self.assertEqual(subtract_rect([(1, 2, 3, 4)], (0, 0, 10, 10)), [])


class BoxIndexTests(unittest.TestCase):
    def test_random_queries_match_brute_force(self) -> None:
        import random

        rng = random.Random(91)
        rects = []
        for i in range(500):
            x0 = rng.randrange(0, 200)
            y0 = rng.randrange(0, 200)
            x1 = x0 + rng.randrange(1, 30)
            y1 = y0 + rng.randrange(1, 30)
            rects.append((x0, y0, x1, y1))

        index = BoxIndex(list(enumerate(rects)))
        for _ in range(100):
            qx0 = rng.randrange(0, 220)
            qy0 = rng.randrange(0, 220)
            q = (qx0, qy0, qx0 + rng.randrange(1, 50), qy0 + rng.randrange(1, 50))
            got = set(index.query(q))
            expected = {
                i for i, r in enumerate(rects) if intersection(r, q) is not None
            }
            self.assertEqual(got, expected)


class EngineSemanticsTests(unittest.TestCase):
    def test_reading_order_top_left_draw_order(self) -> None:
        result = process_page_description(
            {
                "bounds": [0, 0, 300, 120],
                "cells": [cell("a", "r", "c", [0, 0, 300, 120])],
                "primitives": [
                    word("lower-left", [20, 60, 80, 90]),
                    word("upper-right", [120, 10, 220, 40]),
                    word("upper-left", [20, 10, 100, 40]),
                ],
            }
        )
        c = cells_by_id(result)["a"]
        self.assertEqual(c["status"], "ok")
        self.assertEqual(c["text"], "upper-left upper-right lower-left")
        self.assertEqual(
            [e["draw_order"] for e in c["evidence"]], [3, 2, 1]
        )

    def test_edge_touch_does_not_occlude(self) -> None:
        result = process_page_description(
            {
                "bounds": [0, 0, 300, 100],
                "cells": [cell("a", "r", "c", [0, 0, 300, 100])],
                "primitives": [
                    word("可见", [10, 10, 100, 40]),
                    blank([100, 10, 200, 40]),
                ],
            }
        )
        self.assertEqual(cells_by_id(result)["a"]["text"], "可见")

    def test_full_white_cover_is_blank(self) -> None:
        result = process_page_description(
            {
                "bounds": [0, 0, 300, 100],
                "cells": [cell("a", "r", "c", [0, 0, 300, 100])],
                "primitives": [
                    word("SECRET-FULL", [10, 10, 100, 40]),
                    blank([0, 0, 200, 100]),
                ],
            }
        )
        c = cells_by_id(result)["a"]
        self.assertEqual(c["status"], "blank")
        self.assertEqual(c["text"], "")
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("SECRET-FULL", serialized)

    def test_later_word_replaces_earlier_word(self) -> None:
        result = process_page_description(
            {
                "bounds": [0, 0, 300, 100],
                "cells": [cell("a", "r", "c", [0, 0, 300, 100])],
                "primitives": [
                    word("SECRET-OLD", [10, 10, 100, 40]),
                    word("新词", [10, 10, 100, 40]),
                ],
            }
        )
        c = cells_by_id(result)["a"]
        self.assertEqual(c["text"], "新词")
        self.assertNotIn("SECRET-OLD", json.dumps(result, ensure_ascii=False))

    def test_union_of_later_boxes_can_fully_hide(self) -> None:
        result = process_page_description(
            {
                "bounds": [0, 0, 200, 100],
                "cells": [cell("a", "r", "c", [0, 0, 200, 100])],
                "primitives": [
                    word("SECRET-UNION", [0, 0, 100, 10]),
                    blank([0, 0, 60, 10]),
                    blank([40, 0, 100, 10]),
                ],
            }
        )
        self.assertEqual(cells_by_id(result)["a"]["status"], "blank")

    def test_partial_cover_makes_whole_cell_undecidable(self) -> None:
        result = process_page_description(
            {
                "bounds": [0, 0, 500, 120],
                "cells": [cell("a", "r", "c", [0, 0, 500, 120])],
                "primitives": [
                    word("OTHER", [300, 50, 360, 80]),
                    word("SECRET-PARTIAL", [10, 10, 200, 40]),
                    blank([10, 10, 80, 40]),
                ],
            }
        )
        c = cells_by_id(result)["a"]
        self.assertEqual(c["status"], "undecidable")
        self.assertIsNone(c["text"])
        self.assertEqual(c["evidence"], [])
        self.assertEqual(c["reasons"][0]["code"], "partial_occlusion_visible_area")
        serialized = json.dumps(c, ensure_ascii=False)
        self.assertNotIn("SECRET-PARTIAL", serialized)
        self.assertNotIn("OTHER", serialized)
        self.assertNotIn("word_id", serialized)

    def test_partial_visible_fragment_only_invalidates_intersected_cell(self) -> None:
        left = cell("L", "r", "c1", [0, 0, 100, 100])
        right = cell("R", "r", "c2", [100, 0, 200, 100])
        result = process_page_description(
            {
                "bounds": [0, 0, 200, 100],
                "cells": [left, right],
                "primitives": [
                    word("SECRET", [50, 20, 150, 80]),
                    blank([50, 20, 100, 80]),
                ],
            }
        )
        cm = cells_by_id(result)
        self.assertEqual(cm["L"]["status"], "blank")
        self.assertEqual(cm["R"]["status"], "undecidable")

    def test_fully_visible_cross_cell_word_invalidates_all_intersected(self) -> None:
        result = process_page_description(
            {
                "bounds": [0, 0, 220, 100],
                "cells": [
                    cell("L", "r", "c1", [0, 0, 100, 100]),
                    cell("R", "r", "c2", [120, 0, 220, 100]),
                ],
                "primitives": [word("CROSS-SECRET", [80, 20, 140, 80])],
            }
        )
        cm = cells_by_id(result)
        self.assertEqual(cm["L"]["status"], "undecidable")
        self.assertEqual(cm["R"]["status"], "undecidable")
        self.assertNotIn("CROSS-SECRET", json.dumps(result))

    def test_exact_cell_fit_is_allowed(self) -> None:
        rect = [10, 20, 110, 60]
        result = process_page_description(
            {
                "bounds": [0, 0, 200, 100],
                "cells": [cell("a", "r", "c", rect)],
                "primitives": [word("恰好", rect)],
            }
        )
        self.assertEqual(cells_by_id(result)["a"]["text"], "恰好")


class ValidationTests(unittest.TestCase):
    def test_positive_area_cell_overlap_is_fatal(self) -> None:
        with self.assertRaises(PageDescriptionError) as ctx:
            process_page_description(
                {
                    "bounds": [0, 0, 300, 300],
                    "cells": [
                        cell("a", "r", "c1", [0, 0, 100, 100]),
                        cell("b", "r", "c2", [90, 10, 190, 110]),
                    ],
                    "primitives": [],
                }
            )
        self.assertEqual(ctx.exception.details.get("cell_id"), "a")

    def test_edge_touching_cells_are_not_overlap(self) -> None:
        result = process_page_description(
            {
                "bounds": [0, 0, 200, 100],
                "cells": [
                    cell("a", "r", "c1", [0, 0, 100, 100]),
                    cell("b", "r", "c2", [100, 0, 200, 100]),
                ],
                "primitives": [],
            }
        )
        self.assertEqual(len(result["cells"]), 2)

    def test_duplicate_page_cell_id_is_fatal(self) -> None:
        with self.assertRaises(PageDescriptionError):
            PageBuilder([0, 0, 100, 100]).add_cell(
                [0, 0, 10, 10], cell_id="x", row_id="r", col_id="c1"
            )
            # The second call happens on one builder in this direct test.
            builder = PageBuilder([0, 0, 100, 100])
            builder.add_cell([0, 0, 10, 10], cell_id="x", row_id="r", col_id="c1")
            builder.add_cell([20, 0, 30, 10], cell_id="x", row_id="r", col_id="c2")

    def test_out_of_page_primitive_is_fatal(self) -> None:
        with self.assertRaises(PageDescriptionError) as ctx:
            process_page_description(
                {
                    "bounds": [0, 0, 100, 100],
                    "cells": [cell("a", "r", "c", [0, 0, 100, 100])],
                    "primitives": [word("x", [90, 10, 101, 20])],
                }
            )
        self.assertEqual(ctx.exception.code, "element_out_of_page")

    def test_unknown_layer_is_fatal(self) -> None:
        with self.assertRaises(PageDescriptionError):
            process_page_description(
                {
                    "bounds": [0, 0, 100, 100],
                    "cells": [cell("a", "r", "c", [0, 0, 100, 100])],
                    "primitives": [{"type": "raster", "rect": [0, 0, 1, 1]}],
                }
            )

    def test_non_integer_coordinates_are_fatal(self) -> None:
        with self.assertRaises(PageDescriptionError):
            process_page_description(
                {
                    "bounds": [0, 0, 100, 100],
                    "cells": [cell("a", "r", "c", [0, 0, 100.5, 100])],
                    "primitives": [],
                }
            )


class StreamingAndStagingTests(unittest.TestCase):
    def test_canonical_book_rows_and_privacy(self) -> None:
        secrets = PRIVACY_SECRETS
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            book = write_canonical_book(td_path / "book.jsonl")
            db = td_path / "staging.sqlite3"
            store = StagingStore(db)
            pages = list(process_jsonl(book, store))
            self.assertEqual(len(pages), 2)

            rows = list(iter_business_rows(store))
            self.assertEqual(len(rows), len(CANONICAL_EXPECTED["rows"]))
            actual = {r["row_id"]: r["text_by_col"] for r in rows}
            self.assertEqual(actual, CANONICAL_EXPECTED["rows"])

            rows_buf = io.StringIO()
            cells_buf = io.StringIO()
            wide_csv_buf = io.StringIO()
            self.assertEqual(write_rows_jsonl(store, rows_buf), 7)
            self.assertEqual(write_business_rows_csv(store, wide_csv_buf), 7)
            write_cells_jsonl(store, cells_buf)
            self.assertIn("c1__status", wide_csv_buf.getvalue())
            self.assertIn("undecidable", wide_csv_buf.getvalue())
            store.conn.commit()
            store.close()

            blob = rows_buf.getvalue() + cells_buf.getvalue()
            blob += wide_csv_buf.getvalue()
            blob += db.read_bytes().decode("utf-8", errors="ignore")
            for secret in secrets:
                self.assertNotIn(secret, blob)

    def test_global_duplicate_cell_across_pages_is_fatal(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            paths = write_bad_fixtures(td)
            store = StagingStore(Path(td) / "dup.sqlite3")
            with self.assertRaises(PageDescriptionError) as ctx:
                list(process_jsonl(paths["duplicate"], store))
            self.assertEqual(ctx.exception.code, "duplicate_logical_cell")
            store.close()

    def test_small_large_generator_runs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = write_large_book(
                Path(td) / "small.jsonl",
                pages=1,
                rows_per_page=4,
                cols=2,
                words_per_cell=2,
            )
            result = next(process_jsonl(path))
            self.assertEqual(result["stats"]["cell_count"], 8)
            self.assertGreater(result["stats"]["word_count"], 8)


if __name__ == "__main__":
    unittest.main(verbosity=2)
