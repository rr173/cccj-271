#!/usr/bin/env python3
"""Re-runnable end-to-end check for the persistent roster archive.

Runs the real CLI to:

1. submit dated rosters out of effective-date order,
2. ask a cutoff query that must not change when a later batch arrives,
3. verify a blank / undecidable cell is not backfilled by an older value,
4. replace one date wholesale, retract another, then re-submit,
5. confirm rejected ingests change nothing, and
6. confirm no covered secret appears in archive bytes or command output.

Usage::

    python3 -I /workspace/verify_archive.py [--workdir DIR]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CLI = ROOT / "roster.py"
SECRET = "覆盖秘密VERIFY"


def roster_file(path: Path, cells: list[dict]) -> Path:
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in _page(cells)) + "\n",
        encoding="utf-8",
    )
    return path


def _page(cells: list[dict]) -> list[dict]:
    width = max(c["rect"][2] for c in cells)
    height = max(c["rect"][3] for c in cells)
    records: list[dict] = [
        {"type": "page_start", "page_id": "P1", "page_no": 1,
         "bounds": [0, 0, width, height]}
    ]
    for index, cell in enumerate(cells):
        records.append(
            {
                "type": "cell",
                "cell_id": f"cell-{index}",
                "row_id": cell["row_id"],
                "col_id": cell["col_id"],
                "reading_order": index + 1,
                "rect": cell["rect"],
            }
        )
    for cell in cells:
        for word in cell.get("words", []):
            records.append({"type": "word", "text": word[0], "rect": word[1]})
        for blank in cell.get("blanks", []):
            records.append({"type": "blank", "rect": blank})
    records.append({"type": "page_end", "page_id": "P1"})
    return records


def cell(row: str, col: str, rect: list[int], words=(), blanks=()) -> dict:
    return {"row_id": row, "col_id": col, "rect": rect,
            "words": list(words), "blanks": list(blanks)}


def run(*args: str) -> tuple[int, str, str]:
    proc = subprocess.run(
        [sys.executable, "-I", str(CLI), *args],
        capture_output=True, text=True,
    )
    return proc.returncode, proc.stdout, proc.stderr


def ingest(db: Path, batch: int, date: str, *,
           action: str = "submit", roster: Path | None = None) -> dict:
    args = ["ingest", "--archive", str(db), "--batch", str(batch),
            "--effective-date", date, "--action", action]
    if roster is not None:
        args += ["--roster", str(roster)]
    code, out, err = run(*args)
    if code != 0:
        raise SystemExit(f"ingest failed ({code}): {err.strip()}")
    return json.loads(out)


def query(db: Path, cutoff: int, date: str, row: str, col: str) -> dict:
    code, out, err = run("query", "--archive", str(db), "--cutoff", str(cutoff),
                         "--date", date, "--row", row, "--col", col)
    if code != 0:
        raise SystemExit(f"query failed ({code}): {err.strip()}")
    return json.loads(out)


class Checks:
    def __init__(self) -> None:
        self.failures = 0

    def eq(self, label: str, actual: object, expected: object) -> None:
        if actual == expected:
            print(f"ok   {label}")
        else:
            self.failures += 1
            print(f"FAIL {label}: got {actual!r}, want {expected!r}")

    def true(self, label: str, condition: bool) -> None:
        self.eq(label, bool(condition), True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--workdir",
        help="run against this directory instead of a temporary one "
        "(the archive at DIR/archive.sqlite3 is recreated)",
    )
    args = parser.parse_args()

    tmp_ctx = None
    if args.workdir:
        workdir = Path(args.workdir)
        workdir.mkdir(parents=True, exist_ok=True)
    else:
        tmp_ctx = tempfile.TemporaryDirectory(prefix="archive-verify-")
        workdir = Path(tmp_ctx.name)

    db = workdir / "archive.sqlite3"
    if db.exists():
        db.unlink()

    checks = Checks()
    outputs: list[str] = []

    try:
        # --- batch 1: 2026-02-01 (name + addr) -------------------------
        r1 = roster_file(workdir / "r1.jsonl", [
            cell("R1", "name", [0, 0, 200, 60], [("一月", [10, 10, 90, 40])]),
            cell("R1", "addr", [0, 60, 200, 120], [("旧地址", [10, 70, 90, 100])]),
        ])
        info = ingest(db, 1, "2026-02-01", roster=r1)
        checks.eq("batch 1 cell_count", info["cell_count"], 2)

        # --- batch 2: 2026-01-01, earlier effective date --------------
        r2 = roster_file(workdir / "r2.jsonl", [
            cell("R1", "name", [0, 0, 200, 60], [("更早", [10, 10, 90, 40])]),
        ])
        ingest(db, 2, "2026-01-01", roster=r2)

        # --- batch 3: 2026-03-01 with a covered secret, blank and undecidable
        r3 = roster_file(workdir / "r3.jsonl", [
            cell("R1", "name", [0, 0, 200, 60], [],
                 blanks=[[0, 0, 200, 60]]),
            cell("R1", "addr", [0, 60, 200, 120],
                 [(SECRET, [10, 70, 150, 100])],
                 blanks=[[10, 70, 80, 100]]),
            cell("R1", "extra", [0, 120, 200, 180],
                 [("可公开", [10, 130, 90, 160])]),
        ])
        ingest(db, 3, "2026-03-01", roster=r3)

        # Fixed answers at their own cutoffs.
        before_3 = query(db, 2, "2026-02-01", "R1", "name")
        checks.eq("cutoff 2 before later batch", before_3["text"], "一月")

        checks.eq("newest not-later date", query(db, 3, "2026-02-01", "R1", "name")["text"], "一月")
        checks.eq("date fallback to 2026-01-01",
                  query(db, 3, "2026-01-15", "R1", "name")["text"], "更早")
        checks.eq("no record before first date",
                  query(db, 3, "2025-12-31", "R1", "name")["result"], "no_record")
        checks.eq("no record for unknown cell",
                  query(db, 3, "2026-03-01", "R1", "nope")["result"], "no_record")

        blank = query(db, 3, "2026-03-01", "R1", "name")
        checks.eq("blank is a real answer", blank["status"], "blank")
        checks.eq("blank text", blank["text"], "")
        und = query(db, 3, "2026-03-01", "R1", "addr")
        checks.eq("undecidable is a real answer", und["status"], "undecidable")
        checks.eq("undecidable text", und["text"], None)
        checks.eq("undecidable reason",
                  und["reasons"][0]["code"], "partial_occlusion_visible_area")

        # Provenance: which roster supplied the fact.
        prov = query(db, 3, "2026-02-01", "R1", "name")
        checks.eq("provenance effective_date", prov["effective_date"], "2026-02-01")
        checks.eq("provenance batch", prov["ingest_batch_no"], 1)
        checks.eq("provenance page", prov["page_id"], "P1")
        checks.eq("provenance cell", prov["cell_id"], "cell-0")

        # --- batch 4/5: wholesale replacement and retraction ----------
        r4 = roster_file(workdir / "r4.jsonl", [
            cell("R1", "name", [0, 0, 200, 60], [("新版", [10, 10, 90, 40])]),
        ])
        ingest(db, 4, "2026-02-01", roster=r4)  # drops addr for that date
        checks.eq("replacement wins", query(db, 4, "2026-02-01", "R1", "name")["text"], "新版")
        checks.eq("old cutoff unchanged", query(db, 3, "2026-02-01", "R1", "name")["text"], "一月")
        checks.eq("dropped cell falls back",
                  query(db, 4, "2026-03-01", "R1", "addr")["status"], "undecidable")

        ingest(db, 5, "2026-01-01", action="retract")
        checks.eq("retracted date gone", query(db, 5, "2026-01-01", "R1", "name")["result"], "no_record")
        checks.eq("retraction does not touch later dates",
                  query(db, 5, "2026-02-01", "R1", "name")["text"], "新版")
        checks.eq("pre-retraction cutoff still answers",
                  query(db, 4, "2026-01-01", "R1", "name")["text"], "更早")

        # --- rejected ingests change nothing --------------------------
        batches_before = json.loads(run("batches", "--archive", str(db))[1])
        code, _out, err = run("ingest", "--archive", str(db), "--batch", "5",
                              "--effective-date", "2026-04-01", "--roster", str(r1))
        checks.eq("non-increasing batch rejected", code, 2)
        checks.eq("rejection code", json.loads(err)["error_code"], "batch_not_increasing")
        code, _out, err = run("ingest", "--archive", str(db), "--batch", "6",
                              "--effective-date", "2026-04-01", "--action", "retract")
        checks.eq("nothing to retract rejected", json.loads(err)["error_code"],
                  "no_roster_to_retract")
        code, _out, _err = run("ingest", "--archive", str(db), "--batch", "6",
                               "--effective-date", "2026-04-01",
                               "--roster", str(workdir / "missing.jsonl"))
        checks.eq("bad roster rejected", code, 2)
        batches_after = json.loads(run("batches", "--archive", str(db))[1])
        checks.eq("batch table unchanged", batches_after, batches_before)

        # --- re-submit after retraction ------------------------------
        ingest(db, 6, "2026-01-01", roster=r1)
        checks.eq("re-submitted date", query(db, 6, "2026-01-01", "R1", "name")["text"], "一月")

        # --- privacy --------------------------------------------------
        outputs.append(json.dumps(batches_after, ensure_ascii=False))
        for cutoff in range(0, 7):
            for date in ("2026-01-01", "2026-02-01", "2026-03-01"):
                for col in ("name", "addr", "extra"):
                    outputs.append(json.dumps(
                        query(db, cutoff, date, "R1", col), ensure_ascii=False))
        blob = "".join(outputs) + db.read_bytes().decode("utf-8", errors="ignore")
        checks.true("covered secret absent from archive and output",
                    SECRET not in blob)

        print()
        if checks.failures:
            print(f"{checks.failures} check(s) FAILED")
            return 1
        print("all archive checks passed")
        return 0
    finally:
        if tmp_ctx is not None:
            tmp_ctx.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
