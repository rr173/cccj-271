"""Deterministic vector page-description fixtures and a large-book generator."""

from __future__ import annotations

import json
import math
import random
from pathlib import Path
from typing import Any, TextIO

PRIVACY_SECRETS = [
    "全遮秘密ALPHA",
    "旧词BETA",
    "半遮GAMMA",
    "跨界DELTA",
]

CANONICAL_EXPECTED: dict[str, Any] = {
    "rows": {
        "r-normal": {
            "c1": "公开 清册",
            "c2": "业务二列",
        },
        "r-full-white": {
            "c1": "",
            "c2": "",
        },
        "r-replaced-word": {
            "c1": "新词BETA",
            "c2": "",
        },
        "r-partial": {
            "c1": None,
            "c2": "其他单元照常",
        },
        "r-cross-boundary": {
            "c1": None,
            "c2": None,
        },
        "r-cross-page": {
            "c1": "上页部分",
            "c2": "下页部分",
        },
        "r-second-page-normal": {
            "c1": "次页末行",
        },
    },
}


def _write(fh: TextIO, record: dict[str, Any]) -> None:
    fh.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
    fh.write("\n")


def write_canonical_book(path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w", encoding="utf-8") as fh:
        _write(fh, {"type": "book_start", "book_id": "canonical"})

        # ---------------- Page 1 ----------------
        _write(
            fh,
            {
                "type": "page_start",
                "page_id": "P1",
                "page_no": 1,
                "bounds": [0, 0, 900, 620],
            },
        )

        cells_p1 = [
            ("r1c1", "r-normal", "c1", 1, 1, 10, [20, 20, 420, 90]),
            ("r1c2", "r-normal", "c2", 1, 2, 20, [440, 20, 860, 90]),
            ("r2c1", "r-full-white", "c1", 2, 1, 30, [20, 110, 420, 180]),
            ("r2c2", "r-full-white", "c2", 2, 2, 40, [440, 110, 860, 180]),
            ("r3c1", "r-replaced-word", "c1", 3, 1, 50, [20, 200, 420, 270]),
            ("r3c2", "r-replaced-word", "c2", 3, 2, 60, [440, 200, 860, 270]),
            ("r4c1", "r-partial", "c1", 4, 1, 70, [20, 290, 420, 360]),
            ("r4c2", "r-partial", "c2", 4, 2, 80, [440, 290, 860, 360]),
            ("r5c1", "r-cross-boundary", "c1", 5, 1, 90, [20, 380, 420, 450]),
            ("r5c2", "r-cross-boundary", "c2", 5, 2, 100, [440, 380, 860, 450]),
            ("x-c1", "r-cross-page", "c1", 6, 1, 110, [20, 470, 420, 540]),
        ]
        for cid, rid, col, rno, cno, order, rect in cells_p1:
            _write(
                fh,
                {
                    "type": "cell",
                    "cell_id": cid,
                    "row_id": rid,
                    "col_id": col,
                    "row_no": rno,
                    "col_no": cno,
                    "reading_order": order,
                    "rect": rect,
                },
            )

        # r-normal: two words, exact top/left ordering.
        _write(fh, {"type": "word", "word_id": "w1", "text": "公开",
                    "rect": [40, 35, 120, 75]})
        _write(fh, {"type": "word", "word_id": "w2", "text": "清册",
                    "rect": [140, 35, 220, 75]})
        _write(fh, {"type": "word", "word_id": "w3", "text": "业务二列",
                    "rect": [460, 35, 620, 75]})

        # Full white cover: secret must disappear and cell is blank.
        _write(fh, {"type": "word", "word_id": "secret-full",
                    "text": "全遮秘密ALPHA", "rect": [40, 125, 260, 165]})
        _write(fh, {"type": "blank", "blank_id": "b-full",
                    "rect": [30, 115, 280, 175]})

        # New opaque word exactly covers old secret word.
        _write(fh, {"type": "word", "word_id": "secret-old",
                    "text": "旧词BETA", "rect": [40, 215, 260, 255]})
        _write(fh, {"type": "word", "word_id": "replacement",
                    "text": "新词BETA", "rect": [40, 215, 260, 255]})

        # Partial cover makes r4c1 undecidable, including its other word.
        _write(fh, {"type": "word", "word_id": "other-in-invalid-cell",
                    "text": "同行可见", "rect": [300, 305, 400, 345]})
        _write(fh, {"type": "word", "word_id": "secret-partial",
                    "text": "半遮GAMMA", "rect": [40, 305, 260, 345]})
        _write(fh, {"type": "blank", "blank_id": "b-partial",
                    "rect": [40, 305, 140, 345]})
        _write(fh, {"type": "word", "word_id": "r4c2-word",
                    "text": "其他单元照常", "rect": [460, 305, 700, 345]})

        # Fully visible word crosses two positive-area cells.
        _write(fh, {"type": "word", "word_id": "cross-word",
                    "text": "跨界DELTA", "rect": [380, 395, 480, 435]})

        # First page part of a business row that continues on page 2.
        _write(fh, {"type": "word", "word_id": "cross-page-1",
                    "text": "上页部分", "rect": [40, 485, 180, 525]})

        _write(fh, {"type": "page_end", "page_id": "P1"})

        # ---------------- Page 2 ----------------
        _write(
            fh,
            {
                "type": "page_start",
                "page_id": "P2",
                "page_no": 2,
                "bounds": [0, 0, 900, 300],
            },
        )
        _write(
            fh,
            {
                "type": "cell",
                "cell_id": "x-c2",
                "row_id": "r-cross-page",
                "col_id": "c2",
                "row_no": 6,
                "col_no": 2,
                "reading_order": 10,
                "rect": [440, 20, 860, 90],
            },
        )
        _write(
            fh,
            {
                "type": "cell",
                "cell_id": "p2-c1",
                "row_id": "r-second-page-normal",
                "col_id": "c1",
                "row_no": 7,
                "col_no": 1,
                "reading_order": 20,
                "rect": [20, 120, 420, 190],
            },
        )
        _write(fh, {"type": "word", "word_id": "cross-page-2",
                    "text": "下页部分", "rect": [460, 35, 600, 75]})
        _write(fh, {"type": "word", "word_id": "p2-normal",
                    "text": "次页末行", "rect": [40, 135, 180, 175]})
        _write(fh, {"type": "page_end", "page_id": "P2"})
        _write(fh, {"type": "book_end"})

    expected_path = path.with_name("expected.json")
    expected_path.write_text(
        json.dumps(CANONICAL_EXPECTED, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def write_large_book(
    path: str | Path,
    *,
    pages: int = 20,
    rows_per_page: int = 60,
    cols: int = 6,
    words_per_cell: int = 4,
    seed: int = 20261124,
    occlude_probability: float = 0.08,
) -> Path:
    """Stream a deterministic large vector book.

    The generator keeps no book-sized object in memory.  It also never creates
    a pixel grid; all fixtures are integer rectangles.
    """

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)

    word_w = 54
    word_h = 20
    gap_x = 8
    gap_y = 6
    inner_x = 12
    inner_y = 12
    per_line = max(1, (200 - 2 * inner_x + gap_x) // (word_w + gap_x))
    lines = max(1, math.ceil(words_per_cell / per_line))
    cell_h = max(70, 2 * inner_y + lines * word_h + (lines - 1) * gap_y)
    pitch_x = 210
    pitch_y = cell_h + 10
    page_w = 30 + cols * pitch_x
    page_h = 30 + rows_per_page * pitch_y

    with path.open("w", encoding="utf-8") as fh:
        _write(fh, {"type": "book_start", "generator": "large", "seed": seed})

        for page_index in range(pages):
            page_id = f"P{page_index + 1:05d}"
            _write(
                fh,
                {
                    "type": "page_start",
                    "page_id": page_id,
                    "page_no": page_index + 1,
                    "bounds": [0, 0, page_w, page_h],
                },
            )

            # Declare all cells first.
            for r in range(rows_per_page):
                global_row = page_index * rows_per_page + r + 1
                y0 = 20 + r * pitch_y
                for c in range(cols):
                    x0 = 20 + c * pitch_x
                    _write(
                        fh,
                        {
                            "type": "cell",
                            "cell_id": f"{page_id}-cell-{r:04d}-{c:02d}",
                            "row_id": f"row-{global_row:06d}",
                            "col_id": f"col-{c + 1:02d}",
                            "row_no": global_row,
                            "col_no": c + 1,
                            "reading_order": r * cols + c + 1,
                            "rect": [x0, y0, x0 + 200, y0 + cell_h],
                        },
                    )

            seq = 0
            for r in range(rows_per_page):
                global_row = page_index * rows_per_page + r + 1
                y0 = 20 + r * pitch_y
                for c in range(cols):
                    x0 = 20 + c * pitch_x
                    for w in range(words_per_cell):
                        line = w // per_line
                        col_in_line = w % per_line
                        wx0 = x0 + inner_x + col_in_line * (word_w + gap_x)
                        wy0 = y0 + inner_y + line * (word_h + gap_y)
                        wx1 = wx0 + word_w
                        wy1 = wy0 + word_h
                        rect = [wx0, wy0, wx1, wy1]
                        word_id = f"{page_id}-w{seq:08d}"
                        _write(
                            fh,
                            {
                                "type": "word",
                                "word_id": word_id,
                                "text": f"R{global_row}C{c + 1}W{w + 1}",
                                "rect": rect,
                            },
                        )
                        seq += 1

                        roll = rng.random()
                        if roll < occlude_probability * 0.35:
                            _write(
                                fh,
                                {
                                    "type": "blank",
                                    "rect": [wx0 - 2, wy0 - 2, wx1 + 2, wy1 + 2],
                                },
                            )
                            seq += 1
                        elif roll < occlude_probability * 0.75:
                            cut = wx0 + max(1, int(word_w * rng.uniform(0.35, 0.7)))
                            _write(
                                fh,
                                {"type": "blank", "rect": [wx0, wy0, cut, wy1]},
                            )
                            seq += 1
                        elif roll < occlude_probability:
                            _write(
                                fh,
                                {
                                    "type": "word",
                                    "word_id": f"{word_id}-new",
                                    "text": f"N{seq}",
                                    "rect": rect,
                                },
                            )
                            seq += 1

            _write(fh, {"type": "page_end", "page_id": page_id})

        _write(fh, {"type": "book_end"})

    return path


def write_bad_fixtures(outdir: str | Path) -> dict[str, Path]:
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}

    overlap = outdir / "bad_overlap.jsonl"
    with overlap.open("w", encoding="utf-8") as fh:
        _write(fh, {"type": "page_start", "page_id": "bad-overlap",
                    "bounds": [0, 0, 500, 300]})
        _write(fh, {"type": "cell", "cell_id": "a", "row_id": "r", "col_id": "c1",
                    "rect": [10, 10, 200, 100]})
        _write(fh, {"type": "cell", "cell_id": "b", "row_id": "r", "col_id": "c2",
                    "rect": [150, 20, 300, 120]})
        _write(fh, {"type": "page_end"})
    paths["overlap"] = overlap

    duplicate = outdir / "bad_duplicate_cell.jsonl"
    with duplicate.open("w", encoding="utf-8") as fh:
        _write(fh, {"type": "page_start", "page_id": "p1",
                    "bounds": [0, 0, 500, 300]})
        _write(fh, {"type": "cell", "cell_id": "same", "row_id": "r1",
                    "col_id": "c1", "rect": [10, 10, 200, 100]})
        _write(fh, {"type": "page_end"})
        _write(fh, {"type": "page_start", "page_id": "p2",
                    "bounds": [0, 0, 500, 300]})
        _write(fh, {"type": "cell", "cell_id": "same", "row_id": "r2",
                    "col_id": "c1", "rect": [10, 10, 200, 100]})
        _write(fh, {"type": "page_end"})
    paths["duplicate"] = duplicate

    out_of_page = outdir / "bad_out_of_page.jsonl"
    with out_of_page.open("w", encoding="utf-8") as fh:
        _write(fh, {"type": "page_start", "page_id": "bad-oop",
                    "bounds": [0, 0, 500, 300]})
        _write(fh, {"type": "cell", "cell_id": "a", "row_id": "r", "col_id": "c",
                    "rect": [10, 10, 200, 100]})
        _write(fh, {"type": "word", "word_id": "w", "text": "越界",
                    "rect": [10, 20, 501, 80]})
        _write(fh, {"type": "page_end"})
    paths["out_of_page"] = out_of_page

    bad_layer = outdir / "bad_layer.jsonl"
    with bad_layer.open("w", encoding="utf-8") as fh:
        _write(fh, {"type": "page_start", "page_id": "bad-layer",
                    "bounds": [0, 0, 500, 300]})
        _write(fh, {"type": "cell", "cell_id": "a", "row_id": "r", "col_id": "c",
                    "rect": [10, 10, 200, 100]})
        _write(fh, {"type": "raster_image", "path": "not/processed.png",
                    "rect": [10, 10, 100, 100]})
        _write(fh, {"type": "page_end"})
    paths["bad_layer"] = bad_layer

    return paths
