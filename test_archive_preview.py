from __future__ import annotations

import datetime
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from roster_reconstruct import archive  # noqa: E402
from roster_reconstruct.errors import ArchiveError  # noqa: E402
from roster_reconstruct.fixtures import write_dated_roster  # noqa: E402


SECRET = "预览秘密PREVIEW"


def name_cell(text: str, *, row: str = "R1", y: int = 0) -> dict:
    return {"row_id": row, "col_id": "name", "rect": [0, y, 200, y + 60],
            "words": [{"text": text, "rect": [10, y + 10, 90, y + 40]}]}


def addr_cell(text: str, *, row: str = "R1", y: int = 60) -> dict:
    return {"row_id": row, "col_id": "addr", "rect": [0, y, 200, y + 60],
            "words": [{"text": text, "rect": [10, y + 10, 90, y + 40]}]}


class PreviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.td = Path(self._tmp.name)
        self.db = self.td / "archive.sqlite3"
        self._seq = 0

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _roster(self, cells: list[dict]) -> Path:
        self._seq += 1
        return write_dated_roster(self.td / f"r{self._seq}.jsonl", cells=cells)

    def _submit(self, batch: int, date: str, cells: list[dict]) -> dict:
        return archive.ingest(self.db, batch_no=batch, effective_date=date,
                              roster_path=self._roster(cells))

    def _preview(self, batch: int, date: str, *, action: str = "submit",
                 cells: list[dict] | None = None) -> dict:
        roster = self._roster(cells) if cells is not None else None
        return archive.preview(self.db, batch_no=batch, effective_date=date,
                               action=action, roster_path=roster)

    def _query(self, cutoff: int, date: str, col: str, row: str = "R1") -> dict:
        return archive.query(self.db, cutoff_batch_no=cutoff,
                             business_date=date, row_id=row, col_id=col)

    # ------------------------------------------------------------------
    def test_interval_bounded_by_next_effective_roster(self) -> None:
        self._submit(1, "2026-01-01", [name_cell("一")])
        self._submit(2, "2026-02-01", [name_cell("二")])
        self._submit(3, "2026-03-01", [name_cell("三")])

        res = self._preview(4, "2026-02-01", cells=[name_cell("二改")])
        self.assertEqual(res["changed_cell_count"], 1)
        rec = res["records"][0]
        self.assertEqual(rec["interval"], {"start": "2026-02-01",
                                           "end": "2026-03-01"})
        self.assertEqual(rec["before"]["text"], "二")
        self.assertEqual(rec["after"]["text"], "二改")
        self.assertEqual(rec["after"]["ingest_batch_no"], 4)

    def test_unbounded_when_no_later_record(self) -> None:
        self._submit(1, "2026-01-01", [name_cell("一")])
        res = self._preview(2, "2026-01-01", cells=[name_cell("一改")])
        rec = res["records"][0]
        self.assertIsNone(rec["interval"]["end"])

    def test_no_record_before_start(self) -> None:
        self._submit(1, "2026-02-01", [name_cell("二")])
        res = self._preview(2, "2026-01-01", cells=[name_cell("一")])
        rec = res["records"][0]
        self.assertEqual(rec["before"], {"found": False, "result": "no_record"})
        self.assertEqual(rec["after"]["text"], "一")

    def test_dropped_cell_falls_back_to_older_date(self) -> None:
        self._submit(1, "2026-01-01", [name_cell("一"), addr_cell("旧")])
        self._submit(2, "2026-02-01", [name_cell("二"), addr_cell("新")])
        res = self._preview(3, "2026-02-01", cells=[name_cell("二改")])
        rec = next(r for r in res["records"] if r["col_id"] == "addr")
        self.assertEqual(rec["before"]["text"], "新")
        self.assertEqual(rec["after"]["text"], "旧")  # falls back to 01-01
        self.assertEqual(rec["after"]["effective_date"], "2026-01-01")
        self.assertIsNone(rec["interval"]["end"])

    def test_retract_interval_and_fallback(self) -> None:
        self._submit(1, "2026-01-01", [name_cell("一")])
        self._submit(2, "2026-02-01", [name_cell("二")])
        self._submit(3, "2026-03-01", [name_cell("三")])
        res = self._preview(4, "2026-02-01", action="retract")
        rec = res["records"][0]
        self.assertEqual(rec["action"], "retract")
        self.assertEqual(rec["before"]["text"], "二")
        self.assertEqual(rec["after"]["text"], "一")
        self.assertEqual(rec["interval"], {"start": "2026-02-01",
                                           "end": "2026-03-01"})

    def test_blank_undecidable_and_no_record_stay_distinct(self) -> None:
        self._submit(1, "2026-01-01", [
            name_cell("字"),
            addr_cell("地址"),
        ])
        # Blank name, undecidable addr via a half cover.
        self._submit(2, "2026-02-01", [
            {"row_id": "R1", "col_id": "name", "rect": [0, 0, 200, 60]},
            {"row_id": "R1", "col_id": "addr", "rect": [0, 60, 200, 120],
             "words": [{"text": SECRET, "rect": [10, 70, 150, 100]}],
             "blanks": [[10, 70, 80, 100]]},
        ])
        # Preview replacing 02-01 with another blank/undecidable pair; the
        # point here is that the preview carries the distinct answers.
        res = self._preview(3, "2026-02-01", cells=[
            {"row_id": "R1", "col_id": "name", "rect": [0, 0, 200, 60]},
            {"row_id": "R1", "col_id": "addr", "rect": [0, 60, 200, 120],
             "words": [{"text": "另一个秘密", "rect": [10, 70, 150, 100]}],
             "blanks": [[10, 70, 80, 100]]},
        ])
        # Both before and after are undecidable/blank; if content is equal no
        # diff, but the addr cell text must still be null with reasons.
        addr = next((r for r in res["records"] if r["col_id"] == "addr"), None)
        if addr is not None:
            for side in ("before", "after"):
                self.assertTrue(addr[side]["found"])
                self.assertEqual(addr[side]["status"], "undecidable")
                self.assertIsNone(addr[side]["text"])
                self.assertTrue(addr[side]["reasons"])

    def test_only_changed_cells_are_emitted(self) -> None:
        self._submit(1, "2026-01-01", [name_cell("一"), addr_cell("不变")])
        # Same content and same date, but a new batch: provenance changes, so
        # the answer (which includes source) does change.
        res = self._preview(2, "2026-01-01",
                            cells=[name_cell("一"), addr_cell("不变")])
        for rec in res["records"]:
            self.assertNotEqual(rec["before"], rec["after"])

    def test_identical_answer_is_not_emitted(self) -> None:
        self._submit(1, "2026-01-01", [name_cell("一")])
        self._submit(2, "2026-02-01", [name_cell("二")])
        # A submit that only touches an unrelated date must not emit this cell.
        res = self._preview(3, "2026-01-15", cells=[addr_cell("新增")])
        self.assertFalse(any(r["col_id"] == "name" for r in res["records"]))

    def test_deterministic_order(self) -> None:
        self._submit(1, "2026-01-01", [
            name_cell("一", row="R2", y=0), addr_cell("二", row="R2", y=60),
            name_cell("三", row="R1", y=120), addr_cell("四", row="R1", y=180),
        ])
        cells = [
            name_cell("甲", row="R2", y=0), addr_cell("乙", row="R2", y=60),
            name_cell("丙", row="R1", y=120), addr_cell("丁", row="R1", y=180),
        ]
        res = self._preview(2, "2026-01-01", cells=cells)
        keys = [(r["row_id"], r["col_id"], r["interval"]["start"])
                for r in res["records"]]
        self.assertEqual(keys, sorted(keys))
        # Re-running yields byte-identical output.
        first = json.dumps(res["records"], ensure_ascii=False, sort_keys=True)
        again = json.dumps(self._preview(2, "2026-01-01", cells=cells)["records"],
                           ensure_ascii=False, sort_keys=True)
        self.assertEqual(first, again)

    # ------------------------------------------------------------------
    def test_preview_is_read_only_and_does_not_change_old_answers(self) -> None:
        self._submit(1, "2026-01-01", [name_cell("一")])
        batches_before = archive.list_batches(self.db)["batches"]
        bytes_before = self.db.read_bytes()
        q_before = self._query(1, "2026-01-01", "name")

        self._preview(2, "2026-01-01", cells=[name_cell("二")])

        self.assertEqual(archive.list_batches(self.db)["batches"], batches_before)
        self.assertEqual(self.db.read_bytes(), bytes_before)
        self.assertEqual(self._query(1, "2026-01-01", "name"), q_before)
        # Nothing got ingested: cutoff 2 still finds no batch-2 answer.
        self.assertEqual(self._query(2, "2026-01-01", "name")["text"], "一")

    def test_invalid_inputs_leave_no_state_and_no_preview(self) -> None:
        self._submit(1, "2026-01-01", [name_cell("一")])
        bytes_before = self.db.read_bytes()
        batches_before = archive.list_batches(self.db)["batches"]

        cases = [
            dict(batch_no=1, effective_date="2026-02-01",
                 roster_path=self._roster([name_cell("越权")])),
            dict(batch_no=2, effective_date="2026-02-01", action="retract"),
            dict(batch_no=2, effective_date="2026-02-01"),  # missing roster
            dict(batch_no=2, effective_date="2026-02-01", action="submit",
                 roster_path=self._roster([])),  # empty roster
        ]
        for kwargs in cases:
            with self.assertRaises(ArchiveError):
                archive.preview(self.db, **kwargs)
            self.assertEqual(self.db.read_bytes(), bytes_before)
            self.assertEqual(archive.list_batches(self.db)["batches"],
                             batches_before)

    def test_preview_does_not_create_a_missing_archive(self) -> None:
        missing = self.td / "nope.sqlite3"
        with self.assertRaises(ArchiveError) as ctx:
            archive.preview(missing, batch_no=1, effective_date="2026-01-01",
                            roster_path=self._roster([name_cell("一")]))
        self.assertEqual(ctx.exception.code, "archive_not_found")
        self.assertFalse(missing.exists())

    def test_covered_secret_never_appears(self) -> None:
        self._submit(1, "2026-01-01", [
            {"row_id": "R1", "col_id": "name", "rect": [0, 0, 200, 60],
             "words": [{"text": SECRET, "rect": [10, 10, 90, 40]}],
             "blanks": [[0, 0, 100, 60]]},
        ])
        res = self._preview(2, "2026-02-01", cells=[
            {"row_id": "R1", "col_id": "name", "rect": [0, 0, 200, 60],
             "words": [{"text": SECRET, "rect": [10, 10, 90, 40]}],
             "blanks": [[0, 0, 100, 60]]},
        ])
        blob = json.dumps(res, ensure_ascii=False)
        self.assertNotIn(SECRET, blob)

    def test_before_equals_pre_operation_query(self) -> None:
        self._submit(1, "2026-01-01", [name_cell("一"), addr_cell("A")])
        self._submit(2, "2026-02-01", [name_cell("二"), addr_cell("B")])
        self._submit(3, "2026-03-01", [name_cell("三")])

        res = self._preview(4, "2026-02-01",
                            cells=[name_cell("二改"), addr_cell("B2")])
        last = 3
        for rec in res["records"]:
            start = rec["interval"]["start"]
            got = self._query(last, start, rec["col_id"], row=rec["row_id"])
            self.assertEqual(self._normalize(got), rec["before"])

    def test_matches_actual_ingest_day_by_day(self) -> None:
        self._submit(1, "2026-01-01", [name_cell("一"), addr_cell("A")])
        self._submit(2, "2026-02-01", [name_cell("二"), addr_cell("B")])
        self._submit(3, "2026-03-01", [name_cell("三")])

        plans = [
            ("2026-02-01", "submit", [name_cell("二改")]),
            ("2026-02-15", "submit", [addr_cell("C")]),
            ("2026-01-01", "submit", [name_cell("一"), addr_cell("A2")]),
            ("2026-03-01", "retract", []),
        ]
        batch = 4
        for date, action, cells in plans:
            if action == "submit":
                props = archive.preview(self.db, batch_no=batch,
                                        effective_date=date,
                                        roster_path=self._roster(cells))
            else:
                props = archive.preview(self.db, batch_no=batch,
                                        effective_date=date, action=action)
            computed = {(r["row_id"], r["col_id"]): r
                        for r in props["records"]}
            if action == "submit":
                archive.ingest(self.db, batch_no=batch, effective_date=date,
                               roster_path=self._roster(cells))
            else:
                archive.ingest(self.db, batch_no=batch, effective_date=date,
                               action=action)
            self._assert_intervals_hold(date, computed, batch)
            batch += 1

    def _assert_intervals_hold(self, start_date: str, computed: dict,
                               batch: int) -> None:
        lo = datetime.date.fromisoformat(start_date) - datetime.timedelta(days=2)
        hi = datetime.date(2027, 1, 1)
        for (row, col), rec in computed.items():
            start = datetime.date.fromisoformat(rec["interval"]["start"])
            end = hi if rec["interval"]["end"] is None else min(
                hi, datetime.date.fromisoformat(rec["interval"]["end"]))
            day = start
            while day < end:
                if day >= lo:
                    got = self._query(batch, day.isoformat(), col, row=row)
                    got_norm = self._normalize(got)
                    self.assertEqual(got_norm, rec["after"],
                                     f"{row}/{col} @ {day} after preview")
                day += datetime.timedelta(days=1)

    @staticmethod
    def _normalize(q: dict) -> dict:
        if not q["found"]:
            return {"found": False, "result": q["result"]}
        out = {k: q[k] for k in ("found", "status", "text", "effective_date",
                                 "ingest_batch_no", "page_id", "page_no",
                                 "cell_id", "row_no", "col_no")}
        if "reasons" in q:
            out["reasons"] = q["reasons"]
        return out

    def test_work_is_bounded_not_history_replay(self) -> None:
        # 200 distinct effective dates for one cell.
        base = datetime.date(2020, 1, 1)
        for batch in range(1, 201):
            date = (base + datetime.timedelta(days=batch)).isoformat()
            self._submit(batch, date, [name_cell(f"v{batch}")])
        res = self._preview(201, "2020-03-15", cells=[name_cell("X")])
        # Tree height is O(log n): far below the 200 historical batches.
        self.assertLess(res["node_reads"], 60)
        self.assertEqual(res["changed_cell_count"], 1)

    def test_cli_preview_jsonl_and_unchanged_archive(self) -> None:
        self._submit(1, "2026-01-01", [name_cell("一")])
        self._submit(2, "2026-02-01", [name_cell("二")])
        bytes_before = self.db.read_bytes()
        roster = self._roster([name_cell("二改")])
        base = [sys.executable, "-I", str(Path(__file__).parent / "roster.py")]
        proc = subprocess.run(
            base + ["preview", "--archive", str(self.db), "--batch", "3",
                    "--effective-date", "2026-02-01", "--roster", str(roster)],
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = [json.loads(x) for x in proc.stdout.splitlines() if x.strip()]
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["record_type"], "archive_impact")
        self.assertEqual(lines[0]["interval"],
                         {"start": "2026-02-01", "end": None})
        self.assertEqual(lines[0]["after"]["text"], "二改")
        summary = json.loads(proc.stderr)
        self.assertEqual(summary["changed_cell_count"], 1)
        self.assertEqual(self.db.read_bytes(), bytes_before)

    def test_cli_preview_error_exit_code(self) -> None:
        self._submit(1, "2026-01-01", [name_cell("一")])
        roster = self._roster([name_cell("x")])
        base = [sys.executable, "-I", str(Path(__file__).parent / "roster.py")]
        proc = subprocess.run(
            base + ["preview", "--archive", str(self.db), "--batch", "1",
                    "--effective-date", "2026-02-01", "--roster", str(roster)],
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, "")
        self.assertEqual(json.loads(proc.stderr)["error_code"],
                         "batch_not_increasing")


if __name__ == "__main__":
    unittest.main(verbosity=2)
