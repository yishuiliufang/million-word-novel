"""The workflow contract: plan gate, audit-before-commit, transactional commit,
strict round-done, crash recovery, rollback safety and auto-next."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import helpers  # noqa: E402
from helpers import fresh_project, chapter_text  # noqa: E402

import canon_db as db  # noqa: E402
import novel_state  # noqa: E402
import tx as tx_mod  # noqa: E402
import validate_state  # noqa: E402
from _common import (chapter_path, count_chars, load_progress, paths,  # noqa: E402
                     read_text, tolerance)

PY = sys.executable
TESTS = os.path.dirname(os.path.abspath(__file__))


class GateTest(unittest.TestCase):
    def setUp(self):
        self.p = fresh_project(name="gate")
        self.addCleanup(self.p.cleanup)

    def test_commit_without_a_plan_is_refused(self):
        self.p.write_delta(1)
        draft = self.p.write_draft(1)
        self.p.json_run(novel_state, ["audit", "--root", self.p.root, "--chapter", "1",
                                      "--file", draft, "--json"])
        rc, out, err = self.p.commit(1)
        self.assertNotEqual(rc, 0)
        self.assertIn("plan", err)
        self.assertFalse(os.path.isfile(chapter_path(self.p.root, 1)),
                         "a refused commit must not leave a chapter file behind")

    def test_commit_without_an_audit_is_refused(self):
        self.p.write_plan(1)
        self.p.write_delta(1)
        self.p.write_draft(1)
        rc, out, err = self.p.commit(1)
        self.assertNotEqual(rc, 0)
        self.assertIn("audit", err)
        self.assertFalse(os.path.isfile(chapter_path(self.p.root, 1)))

    def test_audit_after_a_text_change_no_longer_authorises_the_commit(self):
        """The audit is pinned to a text hash, so polishing after auditing
        invalidates it. This is what makes `audit -> commit` mechanical."""
        self.p.write_plan(1)
        self.p.write_delta(1)
        draft = self.p.write_draft(1)
        rc, out, err = self.p.json_run(novel_state, ["audit", "--root", self.p.root,
                                                     "--chapter", "1", "--file", draft,
                                                     "--json"])
        self.assertEqual(rc, 0, out)
        with open(draft, "a", encoding="utf-8") as fh:
            fh.write("他又补了一句话。\n")
        rc2, out2, err2 = self.p.commit(1)
        self.assertNotEqual(rc2, 0)
        self.assertIn("审计", err2)

    def test_commit_runs_plan_write_audit_canon_in_order(self):
        self.p.write_plan(1)
        self.p.write_delta(1)
        draft = self.p.write_draft(1)
        self.p.json_run(novel_state, ["audit", "--root", self.p.root, "--chapter", "1",
                                      "--file", draft, "--json"])
        rc, out, err = self.p.commit(1)
        self.assertEqual(rc, 0, err)
        self.assertEqual(out["gates"],
                         {"plan": "pass", "audit": "pass", "canon": "pass",
                          "length": "pass"})
        conn = self.p.conn()
        self.addCleanup(conn.close)
        self.assertEqual(db.get_gates(conn, 1)["audit"], "pass")

    def test_commit_refuses_an_invalid_canon_delta(self):
        self.p.write_plan(1)
        bad = helpers.delta_payload(1)
        # An inverted payoff window is rejected by the delta validator but does
        # not fail the semantic audit — exactly the case the canon gate exists for.
        bad["foreshadowing"] = [{"code": "F001", "action": "plant",
                                 "planned_payoff_start": 40,
                                 "planned_payoff_end": 10}]
        self.p.write_delta(1, bad)
        draft = self.p.write_draft(1)
        self.p.json_run(novel_state, ["audit", "--root", self.p.root, "--chapter", "1",
                                      "--file", draft, "--json"])
        rc, out, err = self.p.commit(1)
        self.assertNotEqual(rc, 0)
        self.assertIn("canon", err)
        self.assertIn("倒置", err)

    def test_waivers_are_explicit_and_reported(self):
        self.p.write_plan(1)
        self.p.write_delta(1)
        self.p.write_draft(1)
        rc, out, err = self.p.commit(1, ["--allow-audit"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(out["gates"]["audit"], "waived")
        self.assertEqual(out["waivers"][0]["gate"], "audit")
        conn = self.p.conn()
        self.addCleanup(conn.close)
        waived = conn.execute("SELECT * FROM warnings WHERE code='GATE_WAIVED'").fetchall()
        self.assertEqual(len(waived), 1)

    def test_length_gate_still_blocks_and_records_a_waiver(self):
        self.p.write_plan(1)
        self.p.write_delta(1)
        self.p.write_draft(1, text="太短了。\n")
        rc, out, err = self.p.commit(1)
        self.assertNotEqual(rc, 0)
        self.assertIn("too short", err)
        # the short text has to be audited (and waived) before it can be committed
        self.p.json_run(novel_state, ["audit", "--root", self.p.root, "--chapter", "1",
                                      "--file", os.path.join(self.p.paths()["drafts"],
                                                             "ch-0001.md")])
        rc2, out2, err2 = self.p.commit(1, ["--allow-short", "--allow-audit"])
        self.assertEqual(rc2, 0, err2)
        self.assertEqual(out2["gates"]["length"], "waived")
        self.assertEqual(out2["gates"]["audit"], "waived")


class RoundDoneTest(unittest.TestCase):
    def setUp(self):
        self.p = fresh_project(name="round")
        self.addCleanup(self.p.cleanup)

    def test_round_done_blocks_when_planned_chapters_are_missing(self):
        brief = self.p.brief()
        self.assertEqual(brief["action"], "WRITE")
        planned = brief["round_planned_chapters"]
        self.assertEqual(len(planned), 3)
        self.p.full_chapter(planned[0])
        rc, out, err = self.p.json_run(novel_state, ["round-done", "--root", self.p.root])
        self.assertEqual(rc, 3, out)
        self.assertEqual(out["verdict"], "ROUND_BLOCKED")
        self.assertFalse(out["round_complete"])
        self.assertTrue(any("缺少" in b for b in out["blockers"]), out["blockers"])
        self.assertEqual(self.p.progress()["rounds_completed"], 0,
                         "a blocked round must not advance the round counter")

    def test_round_done_blocks_when_a_chapter_has_no_audit(self):
        brief = self.p.brief()
        for chapter in brief["round_planned_chapters"][:2]:
            self.p.full_chapter(chapter)
        # chapter 3 committed with a waived audit is recorded, so simulate a
        # chapter that was committed without any audit by waiving it explicitly
        third = brief["round_planned_chapters"][2]
        self.p.write_plan(third)
        self.p.write_delta(third)
        self.p.write_draft(third)
        rc, out, err = self.p.commit(third, ["--allow-audit"])
        self.assertEqual(rc, 0, err)
        rc2, out2, err2 = self.p.json_run(novel_state, ["round-done", "--root", self.p.root])
        self.assertEqual(rc2, 3, out2)
        self.assertTrue(out2["waivers"], "the waiver must be surfaced at round close")

    def test_round_done_blocks_on_an_unhandled_draft(self):
        brief = self.p.brief()
        for chapter in brief["round_planned_chapters"]:
            self.p.full_chapter(chapter)
        self.p.write_draft(9)
        rc, out, err = self.p.json_run(novel_state, ["round-done", "--root", self.p.root])
        self.assertEqual(rc, 3)
        self.assertTrue(any("草稿" in b for b in out["blockers"]), out["blockers"])

    def test_round_done_succeeds_and_advances_after_a_legal_round(self):
        brief = self.p.brief()
        for chapter in brief["round_planned_chapters"]:
            self.p.full_chapter(chapter)
        rc, out, err = self.p.json_run(novel_state, ["round-done", "--root", self.p.root])
        self.assertEqual(rc, 0, out)
        self.assertTrue(out["round_complete"])
        self.assertEqual(out["verdict"], "REINCARNATE")
        self.assertEqual(self.p.progress()["rounds_completed"], 1)
        # the next round is planned and starts after the committed chapters
        brief2 = self.p.brief()
        self.assertEqual(brief2["round_index"], 2)
        self.assertEqual(brief2["chapters_to_write"][0], 4)
        # last-context.md is generated from Canon, not hand-written
        ctx = read_text(self.p.paths()["last_context"])
        self.assertIn("未回收伏笔", ctx)
        self.assertIn("## 下一步", ctx)

    def test_round_done_can_abandon_a_round_with_a_reason(self):
        self.p.brief()
        self.p.write_draft(1)
        rc, out, err = self.p.json_run(novel_state, ["round-done", "--root", self.p.root,
                                                     "--abandon", "--reason", "改大纲"])
        self.assertEqual(rc, 0, out)
        self.assertEqual(out["verdict"], "ROUND_ABANDONED")
        self.assertEqual(self.p.progress()["rounds_completed"], 0)
        self.assertTrue(out["drafts_moved"])
        self.assertFalse(os.path.isfile(os.path.join(self.p.paths()["drafts"],
                                                     "ch-0001.md")))

    def test_round_done_abandon_requires_a_reason(self):
        self.p.brief()
        rc, out, err = self.p.json_run(novel_state, ["round-done", "--root", self.p.root,
                                                     "--abandon"])
        self.assertNotEqual(rc, 0)


class RecoveryTest(unittest.TestCase):
    def setUp(self):
        self.p = fresh_project(name="recover")
        self.addCleanup(self.p.cleanup)

    def test_next_reports_and_clears_pending_work(self):
        self.p.full_chapter(1)
        self.p.write_draft(2)
        brief = self.p.brief()
        self.assertIn(2, brief["recovery"]["uncommitted_drafts"])
        self.assertIn("未提交草稿", brief["recovery"]["resume"])

    def test_recover_command_reports_clean_state(self):
        self.p.full_chapter(1)
        rc, out, err = self.p.json_run(novel_state, ["recover", "--root", self.p.root])
        self.assertEqual(rc, 0)
        self.assertTrue(out["clean"])
        self.assertFalse(out["pending"]["needs_recover"])

    def test_recover_completes_an_interrupted_commit(self):
        conn = self.p.conn()
        txn = tx_mod.CommitTransaction(self.p.root, 5)
        txn.write_text("chapter", chapter_path(self.p.root, 5), "被中断的正文\n")
        txn.mark_db_committed()
        conn.close()
        rc, out, err = self.p.json_run(novel_state, ["recover", "--root", self.p.root])
        self.assertEqual(rc, 0, out)
        self.assertTrue(os.path.isfile(chapter_path(self.p.root, 5)))
        self.assertEqual(out["actions"][0]["action"], "complete_publish")

    def test_detect_half_open_transaction_in_validation(self):
        conn = self.p.conn()
        txn = tx_mod.CommitTransaction(self.p.root, 6)
        txn.write_text("chapter", chapter_path(self.p.root, 6), "半开事务\n")
        conn.close()
        report = validate_state.validate(self.p.root)
        codes = [f["code"] for f in report["findings"]]
        self.assertIn("TRANSACTION_HALF_OPEN", codes)
        self.assertGreater(report["errors"], 0)


class ValidationTest(unittest.TestCase):
    def setUp(self):
        self.p = fresh_project(name="validate")
        for n in (1, 2, 3):
            self.p.full_chapter(n)
        self.addCleanup(self.p.cleanup)

    def test_clean_project_validates(self):
        report = validate_state.validate(self.p.root)
        self.assertEqual(report["errors"], 0, report["findings"])
        self.assertTrue(report["ok"])

    def test_detects_chapter_gap(self):
        os.remove(chapter_path(self.p.root, 2))
        report = validate_state.validate(self.p.root)
        self.assertIn("CHAPTER_GAP", [f["code"] for f in report["findings"]])
        self.assertIn("CHAPTER_ORPHAN_ROW", [f["code"] for f in report["findings"]])
        self.assertFalse(report["ok"])

    def test_detects_a_chapter_file_modified_after_commit(self):
        path = chapter_path(self.p.root, 2)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("偷偷加的一句话。\n")
        report = validate_state.validate(self.p.root)
        codes = [f["code"] for f in report["findings"]]
        self.assertIn("CHAPTER_HASH_MISMATCH", codes)
        self.assertIn("AUDIT_STALE", codes)

    def test_detects_a_missing_snapshot(self):
        os.remove(db.snapshot_path(self.p.root, 3))
        conn = self.p.conn()
        conn.execute("DELETE FROM snapshots WHERE chapter=3 AND kind='delta'")
        conn.close()
        report = validate_state.validate(self.p.root)
        codes = [f["code"] for f in report["findings"]]
        self.assertIn("SNAPSHOT_MISSING", codes)
        self.assertIn("SNAPSHOT_ROW_MISSING", codes)

    def test_detects_dangling_event_participant(self):
        conn = self.p.conn()
        conn.execute("UPDATE events SET participants='[\"幽灵人物\"]' WHERE chapter=2")
        conn.close()
        report = validate_state.validate(self.p.root)
        codes = [f["code"] for f in report["findings"]]
        self.assertIn("EVENT_UNKNOWN_PARTICIPANT", codes)

    def test_detects_illegal_thread_status(self):
        conn = self.p.conn()
        conn.execute("UPDATE plot_threads SET status='weird' WHERE code='T1'")
        conn.close()
        report = validate_state.validate(self.p.root)
        self.assertIn("ILLEGAL_THREAD_STATUS", [f["code"] for f in report["findings"]])

    def test_detects_relationship_to_a_non_character(self):
        # The FK constraint makes a truly dangling row impossible to insert, so
        # the reachable defect is a relationship pointing at a non-character.
        conn = self.p.conn()
        loc = conn.execute("SELECT id FROM entities WHERE kind='location' LIMIT 1"
                           ).fetchone()
        char = conn.execute("SELECT id FROM entities WHERE kind='character' LIMIT 1"
                            ).fetchone()
        conn.execute("INSERT INTO relationships(from_entity,to_entity,kind) VALUES(?,?,?)",
                     (char["id"], loc["id"], "trust"))
        conn.close()
        report = validate_state.validate(self.p.root)
        codes = [f["code"] for f in report["findings"]]
        self.assertIn("RELATIONSHIP_UNKNOWN_CHARACTER", codes)

    def test_detects_timeline_regression(self):
        conn = self.p.conn()
        conn.execute("UPDATE timeline SET world_time='2000-01-01' WHERE chapter=3")
        conn.close()
        report = validate_state.validate(self.p.root)
        self.assertIn("TIMELINE_REGRESSION", [f["code"] for f in report["findings"]])

    def test_detects_foreshadow_window_problems(self):
        conn = self.p.conn()
        conn.execute("UPDATE foreshadowings SET planned_payoff_start=50, "
                     "planned_payoff_end=10 WHERE code='F001'")
        conn.execute("UPDATE foreshadowings SET planned_payoff_start=NULL, "
                     "planned_payoff_end=NULL WHERE code='F002'")
        conn.execute("INSERT INTO foreshadowings(code,title,status,planned_payoff_start,"
                     "planned_payoff_end,data) VALUES('F002','无窗口伏笔','PLANTED',NULL,"
                     "NULL,'{}')")
        conn.close()
        report = validate_state.validate(self.p.root)
        codes = [f["code"] for f in report["findings"]]
        self.assertIn("FORESHADOW_WINDOW_INVERTED", codes)
        self.assertIn("FORESHADOW_NO_WINDOW", codes)

    def test_detects_overdue_foreshadowing(self):
        p = fresh_project(name="overdue", target=60000, chapters_per_round=3)
        self.addCleanup(p.cleanup)
        for n in range(1, 11):
            p.full_chapter(n)
        conn = p.conn()
        conn.execute("UPDATE foreshadowings SET planned_payoff_end=2, "
                     "planned_payoff_start=1, status='ACTIVE' WHERE code='F001'")
        conn.close()
        report = validate_state.validate(p.root)
        self.assertIn("FORESHADOW_OVERDUE", [f["code"] for f in report["findings"]])

    def test_detects_open_round_with_unfinished_tasks(self):
        self.p.brief()
        report = validate_state.validate(self.p.root)
        self.assertIn("ROUND_OPEN_WITH_TASKS", [f["code"] for f in report["findings"]])

    def test_cli_exit_code_is_one_on_errors(self):
        os.remove(chapter_path(self.p.root, 2))
        rc, out, err = self.p.run(validate_state, ["--root", self.p.root])
        self.assertEqual(rc, 1)


class AutoNextTest(unittest.TestCase):
    def setUp(self):
        self.p = fresh_project(name="auto")
        self.addCleanup(self.p.cleanup)

    def test_auto_next_runs_the_whole_pipeline(self):
        writer = '%s "%s" write' % (PY, os.path.join(TESTS, "fake_writer.py"))
        extractor = '%s "%s" extract' % (PY, os.path.join(TESTS, "fake_writer.py"))
        rc, out, err = self.p.run(novel_state, [
            "auto-next", "--root", self.p.root, "--writer", writer,
            "--extractor", extractor, "--max-chapters", "2"])
        self.assertEqual(rc, 0, err or out)
        summary = helpers.last_json(out)
        self.assertEqual(summary["chapters"], 2)
        self.assertTrue(os.path.isfile(chapter_path(self.p.root, 1)))
        self.assertTrue(os.path.isfile(chapter_path(self.p.root, 2)))
        report = validate_state.validate(self.p.root)
        self.assertEqual(report["errors"], 0, report["findings"])
        # the run is observable and resumable
        log = read_text(os.path.join(self.p.paths()["state"], "auto.log"))
        steps = [json.loads(line)["step"] for line in log.strip().split("\n")]
        for expected in ("next", "plan", "write", "extract", "audit", "commit"):
            self.assertIn(expected, steps)
        conn = self.p.conn()
        self.addCleanup(conn.close)
        self.assertEqual([c["number"] for c in db.chapters_all(conn)], [1, 2])

    def test_auto_next_without_a_writer_explains_the_contract(self):
        rc, out, err = self.p.run(novel_state, ["auto-next", "--root", self.p.root])
        self.assertNotEqual(rc, 0)
        self.assertIn("--writer", err)


if __name__ == "__main__":
    unittest.main()
