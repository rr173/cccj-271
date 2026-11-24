from __future__ import annotations

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


SECRETS = ["全遮秘密ARCH", "半遮秘密ARCH", "旧词秘密ARCH"]


def roster(path: Path, cells: list[dict]) -> Path:
    return write_dated_roster(path, cells=cells)


def name_cell(text: str, *, row: str = "R1") -> dict:
    return {
        "row_id": row,
        "col_id": "name",
        "rect": [0, 0, 200, 60],
        "words": [{"text": text, "rect": [10, 10, 90, 40]}],
    }


def addr_cell(text: str, *, row: str = "R1") -> dict:
    return {
        "row_id": row,
        "col_id": "addr",
        "rect": [0, 60, 200, 120],
        "words": [{"text": text, "rect": [10, 70, 90, 100]}],
    }


class ArchiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.td = Path(self._tmp.name)
        self.db = self.td / "archive.sqlite3"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _submit(self, batch: int, date: str, cells: list[dict]) -> dict:
        path = roster(self.td / f"r{batch}-{date}.jsonl", cells)
        return archive.ingest(
            self.db, batch_no=batch, effective_date=date, roster_path=path
        )

    def _query(self, cutoff: int, date: str, col: str, row: str = "R1") -> dict:
        return archive.query(
            self.db,
            cutoff_batch_no=cutoff,
            business_date=date,
            row_id=row,
            col_id=col,
        )

    def test_no_record_for_unknown_cell_or_cutoff(self) -> None:
        self._submit(1, "2026-01-01", [name_cell("公开")])
        before = self._query(0, "2026-01-01", "name")
        missing = self._query(1, "2026-01-01", "nope")
        unknown_row = self._query(1, "2026-01-01", "name", row="R9")
        for result in (before, missing, unknown_row):
            self.assertFalse(result["found"])
            self.assertEqual(result["result"], "no_record")

    def test_query_picks_newest_date_not_later_than_business_date(self) -> None:
        self._submit(1, "2026-01-01", [name_cell("一月")])
        # Ingest order need not follow effective-date order.
        self._submit(2, "2025-12-01", [name_cell("十二月")])
        self._submit(3, "2026-02-01", [name_cell("二月")])

        self.assertEqual(self._query(3, "2025-11-30", "name")["result"], "no_record")
        self.assertEqual(self._query(3, "2025-12-15", "name")["text"], "十二月")
        self.assertEqual(self._query(3, "2026-01-15", "name")["text"], "一月")
        self.assertEqual(self._query(3, "2026-03-01", "name")["text"], "二月")

    def test_old_cutoff_keeps_old_answer_after_new_ingest(self) -> None:
        self._submit(1, "2026-01-01", [name_cell("旧答案")])
        as_of_1 = self._query(1, "2026-01-01", "name")
        self._submit(2, "2026-01-01", [name_cell("新答案")])
        self.assertEqual(as_of_1["text"], "旧答案")
        self.assertEqual(self._query(1, "2026-01-01", "name")["text"], "旧答案")
        self.assertEqual(self._query(2, "2026-01-01", "name")["text"], "新答案")

    def test_resubmit_replaces_wholesale_and_falls_back_to_older_date(self) -> None:
        self._submit(1, "2026-02-01", [name_cell("甲"), addr_cell("旧地址")])
        self._submit(2, "2026-02-10", [name_cell("乙"), addr_cell("新地址")])
        # The next roster for 2026-02-10 omits the address cell entirely.
        self._submit(3, "2026-02-10", [name_cell("丙")])

        self.assertEqual(self._query(3, "2026-02-10", "name")["text"], "丙")
        fallback = self._query(3, "2026-02-10", "addr")
        self.assertEqual(fallback["text"], "旧地址")
        self.assertEqual(fallback["effective_date"], "2026-02-01")
        self.assertEqual(fallback["ingest_batch_no"], 1)
        # The replaced 2026-02-10 address is gone: an older cutoff still sees it.
        self.assertEqual(self._query(2, "2026-02-10", "addr")["text"], "新地址")

    def test_blank_and_undecidable_are_real_results_not_backfilled(self) -> None:
        self._submit(1, "2026-03-01", [
            name_cell("曾经有字"),
            {
                "row_id": "R1",
                "col_id": "addr",
                "rect": [0, 60, 200, 120],
                "words": [{"text": "半遮秘密ARCH", "rect": [10, 70, 150, 100]}],
                "blanks": [[10, 70, 80, 100]],
            },
        ])
        self._submit(2, "2026-03-05", [
            {"row_id": "R1", "col_id": "name", "rect": [0, 0, 200, 60]},
            addr_cell("新地址"),
        ])

        blank = self._query(2, "2026-03-05", "name")
        self.assertEqual(blank["status"], "blank")
        self.assertEqual(blank["text"], "")
        self.assertEqual(blank["effective_date"], "2026-03-05")

        und = self._query(1, "2026-03-05", "addr")
        self.assertEqual(und["status"], "undecidable")
        self.assertIsNone(und["text"])
        self.assertEqual(
            und["reasons"][0]["code"], "partial_occlusion_visible_area"
        )

    def test_retract_then_resubmit(self) -> None:
        self._submit(1, "2026-04-01", [name_cell("首册")])
        self._submit(2, "2026-04-02", [name_cell("次册")])
        archive.ingest(self.db, batch_no=3, effective_date="2026-04-01",
                       action="retract")

        retracted = self._query(3, "2026-04-01", "name")
        self.assertFalse(retracted["found"])
        # 2026-04-02 is unaffected and is not pulled down by an earlier lookup.
        self.assertEqual(self._query(3, "2026-04-02", "name")["text"], "次册")
        # The old cutoff still sees the roster that existed then.
        self.assertEqual(self._query(2, "2026-04-01", "name")["text"], "首册")

        self._submit(4, "2026-04-01", [name_cell("重提")])
        self.assertEqual(self._query(4, "2026-04-01", "name")["text"], "重提")
        self.assertEqual(self._query(4, "2026-04-02", "name")["text"], "次册")

    def test_rejected_ingests_leave_archive_unchanged(self) -> None:
        self._submit(1, "2026-05-01", [name_cell("稳定")])
        before = self._query(10, "2026-05-01", "name")
        batches_before = archive.list_batches(self.db)["batches"]

        with self.assertRaises(ArchiveError) as ctx:
            archive.ingest(self.db, batch_no=1, effective_date="2026-05-02",
                           roster_path=roster(self.td / "same.jsonl",
                                              [name_cell("越权")]))
        self.assertEqual(ctx.exception.code, "batch_not_increasing")

        with self.assertRaises(ArchiveError) as ctx:
            archive.ingest(self.db, batch_no=2, effective_date="2026-05-02",
                           action="retract")
        self.assertEqual(ctx.exception.code, "no_roster_to_retract")

        # A geometrically bad roster fails before the archive is touched.
        bad = self.td / "bad.jsonl"
        bad.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in [
                {"type": "page_start", "page_id": "B", "bounds": [0, 0, 200, 200]},
                {"type": "cell", "cell_id": "a", "row_id": "R1",
                 "col_id": "c1", "rect": [0, 0, 150, 150]},
                {"type": "cell", "cell_id": "b", "row_id": "R1",
                 "col_id": "c2", "rect": [100, 0, 190, 150]},
                {"type": "page_end"},
            ]) + "\n",
            encoding="utf-8",
        )
        with self.assertRaises(Exception):
            archive.ingest(self.db, batch_no=3, effective_date="2026-05-02",
                           roster_path=bad)
        self.assertEqual(archive.list_batches(self.db)["batches"], batches_before)
        after = self._query(10, "2026-05-01", "name")
        self.assertEqual(after, before)

    def test_bad_dates_and_batches_are_rejected(self) -> None:
        path = roster(self.td / "r.jsonl", [name_cell("值")])
        for batch, date in ((0, "2026-06-01"), (-1, "2026-06-01"),
                            (1, "2026-6-1"), (1, "2026-02-30")):
            with self.assertRaises(ArchiveError):
                archive.ingest(self.db, batch_no=batch, effective_date=date,
                               roster_path=path)
        with self.assertRaises(ArchiveError):
            archive.query(self.db, cutoff_batch_no=-1, business_date="2026-06-01",
                          row_id="R1", col_id="name")

    def test_query_is_one_root_lookup_and_one_descent(self) -> None:
        # One cell with many versions; a query must not walk version history.
        for batch in range(1, 61):
            date = f"2026-{1 + batch % 9:02d}-{1 + batch % 28:02d}"
            self._submit(batch, date, [name_cell(f"v{batch}")])
        with archive.ArchiveStore(self.db, read_only=True) as store:
            statements: list[str] = []
            store.conn.set_trace_callback(statements.append)
            result = store.query(
                cutoff_batch_no=60,
                business_date="2026-09-20",
                row_id="R1",
                col_id="name",
            )
            trace = [s for s in statements if s.startswith("SELECT")]
        self.assertTrue(result["found"])
        # Root lookup, treap node reads (bounded by tree height), then stops.
        self.assertLess(len(trace), 40)

    def test_covered_secrets_never_enter_archive_or_output(self) -> None:
        self._submit(1, "2026-07-01", [
            {
                "row_id": "R1",
                "col_id": "name",
                "rect": [0, 0, 200, 60],
                "words": [{"text": "全遮秘密ARCH", "rect": [10, 10, 90, 40]}],
                "blanks": [[0, 0, 100, 60]],
            },
            {
                "row_id": "R1",
                "col_id": "addr",
                "rect": [0, 60, 200, 120],
                "words": [{"text": "旧词秘密ARCH", "rect": [10, 70, 90, 100]}],
                "blanks": [[5, 65, 95, 105]],
            },
        ])
        self._submit(2, "2026-07-01", [
            {
                "row_id": "R1",
                "col_id": "addr",
                "rect": [0, 60, 200, 120],
                "words": [{"text": "旧词秘密ARCH", "rect": [10, 70, 90, 100]}],
                "blanks": [[5, 65, 95, 105]],
            },
        ])
        outputs = json.dumps(archive.list_batches(self.db), ensure_ascii=False)
        for cutoff in (1, 2):
            for col in ("name", "addr"):
                outputs += json.dumps(
                    self._query(cutoff, "2026-07-01", col), ensure_ascii=False
                )
        blob = outputs + self.db.read_bytes().decode("utf-8", errors="ignore")
        for secret in SECRETS:
            self.assertNotIn(secret, blob)


class ArchiveCliTests(unittest.TestCase):
    def test_cli_ingest_query_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            db = td / "cli.sqlite3"
            path = roster(td / "r.jsonl", [name_cell("公开值")])
            base = [sys.executable, "-I", str(Path(__file__).parent / "roster.py")]

            ingest = subprocess.run(
                base + ["ingest", "--archive", str(db), "--batch", "1",
                        "--effective-date", "2026-08-01", "--roster", str(path)],
                capture_output=True, text=True,
            )
            self.assertEqual(ingest.returncode, 0, ingest.stderr)
            self.assertEqual(json.loads(ingest.stdout)["cell_count"], 1)

            query = subprocess.run(
                base + ["query", "--archive", str(db), "--cutoff", "1",
                        "--date", "2026-08-01", "--row", "R1", "--col", "name"],
                capture_output=True, text=True,
            )
            self.assertEqual(query.returncode, 0, query.stderr)
            out = json.loads(query.stdout)
            self.assertTrue(out["found"])
            self.assertEqual(out["text"], "公开值")
            self.assertEqual(out["effective_date"], "2026-08-01")
            self.assertEqual(out["ingest_batch_no"], 1)

            retract = subprocess.run(
                base + ["ingest", "--archive", str(db), "--batch", "2",
                        "--effective-date", "2026-08-01", "--action", "retract"],
                capture_output=True, text=True,
            )
            self.assertEqual(retract.returncode, 0, retract.stderr)

            rejected = subprocess.run(
                base + ["ingest", "--archive", str(db), "--batch", "2",
                        "--effective-date", "2026-08-01", "--roster", str(path)],
                capture_output=True, text=True,
            )
            self.assertEqual(rejected.returncode, 2)
            err = json.loads(rejected.stderr)
            self.assertEqual(err["error_code"], "batch_not_increasing")


if __name__ == "__main__":
    unittest.main(verbosity=2)
