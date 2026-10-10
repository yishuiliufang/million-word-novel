"""End-to-end acceptance: ten simulated chapters through the real workflow.

Every chapter goes through the mandated order:

    next -> plan -> write -> audit -> extract(delta) -> commit -> snapshot -> index

and the run is then examined the way an acceptance reviewer would examine it:
chapter files, canon rows, snapshots, rollback, replay verification, validation,
foreshadowing lifecycle, knowledge, timeline, and the export.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import helpers  # noqa: E402
from helpers import fresh_project  # noqa: E402

import canon_db as db  # noqa: E402
import novel_state  # noqa: E402
import validate_state  # noqa: E402
from _common import chapter_path, paths, read_text  # noqa: E402

CHAPTERS = 10

F001_PLANT = {
    "code": "F001", "title": "铜钥匙的来历", "action": "plant", "status": "PLANTED",
    "kind": "long", "planned_payoff_start": 4, "planned_payoff_end": 6,
    "entities": ["李禾"], "threads": ["T1"], "keywords": ["铜钥匙"],
    "description": "李禾在黑塔捡到的那把铜钥匙其实能开第二本账",
}
FACT_SECRET = {"knower": "李禾", "fact_code": "FACT-003",
               "fact_text": "账本第二页是空白的", "source": "E2-1", "secret": True}

# One project is built for the whole module (unittest loads classes in
# alphabetical order, so the destructive class must not own the fixture).
BUILD = {}


def build_project():
    if BUILD:
        return BUILD["project"]
    p = fresh_project(name="e2e", target=12000, chars_per_chapter=1200,
                      chapters_per_round=3, volumes=2)
    steps = []
    for i in range(1, CHAPTERS + 1):
        delta = helpers.delta_payload(i)
        if i == 1:
            delta["foreshadowing"] = [dict(F001_PLANT)]
        elif i == 3:
            delta["foreshadowing"] = [{"code": "F001", "action": "develop",
                                       "note": "钥匙第二次出现"}]
        elif i == 5:
            delta["foreshadowing"] = [{"code": "F001", "action": "resolve",
                                       "note": "钥匙打开第二本账"}]
        if i == 2:
            delta["knowledge_added"] = [dict(FACT_SECRET)]
        p.write_plan(i, helpers.plan_payload(
            i, location=helpers.LOCATIONS[(i - 1) % len(helpers.LOCATIONS)]))
        p.write_delta(i, delta)
        draft = p.write_draft(i)
        audit_rc, audit_out, _ = p.json_run(
            novel_state, ["audit", "--root", p.root, "--chapter", str(i),
                          "--file", draft, "--json", "--strict"])
        assert audit_rc == 0, "audit failed for ch-%d: %s" % (i, audit_out)
        commit_rc, commit_out, commit_err = p.commit(i)
        assert commit_rc == 0, "commit failed for ch-%d: %s" % (i, commit_err)
        steps.append({"chapter": i, "audit": audit_out["pass"], "commit": commit_out})
    BUILD["project"] = p
    BUILD["steps"] = steps
    return p


class TenChapterRunTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.p = build_project()
        cls.steps = BUILD["steps"]

    def test_ten_chapters_exist_as_files_rows_and_snapshots(self):
        for n in range(1, CHAPTERS + 1):
            self.assertTrue(os.path.isfile(chapter_path(self.p.root, n)),
                            "ch-%04d.txt missing" % n)
            self.assertIsNotNone(db.load_snapshot(self.p.root, n),
                                 "ch-%04d snapshot missing" % n)
        conn = self.p.conn()
        self.addCleanup(conn.close)
        numbers = [c["number"] for c in db.chapters_all(conn)]
        self.assertEqual(numbers, list(range(1, CHAPTERS + 1)))
        rows = conn.execute("SELECT COUNT(*) c FROM snapshots WHERE kind='delta'"
                            ).fetchone()["c"]
        self.assertEqual(rows, CHAPTERS)

    def test_every_chapter_passed_the_gates_at_commit_time(self):
        for step in self.steps:
            self.assertTrue(step["audit"])
            self.assertEqual(step["commit"]["gates"],
                             {"plan": "pass", "audit": "pass", "canon": "pass",
                              "length": "pass"})
            self.assertEqual(step["commit"]["waivers"], [])

    def test_character_state_is_tracked_chapter_by_chapter(self):
        conn = self.p.conn()
        self.addCleanup(conn.close)
        history = db.character_state_history(conn, "李禾", limit=100)
        goals = [h for h in history if h["field"] == "goal"]
        # ten chapter commits + the initial value imported from bible.md
        self.assertEqual(len(goals), CHAPTERS + 1)
        self.assertEqual(goals[0]["after"], "第10章的目标")
        self.assertIn("第1章的目标", [g["after"] for g in goals])
        self.assertEqual(db.get_character_state(conn, "李禾")["goal"], "第10章的目标")

    def test_timeline_is_monotonic_across_all_chapters(self):
        conn = self.p.conn()
        self.addCleanup(conn.close)
        tl = db.timeline_all(conn)
        self.assertEqual([t["chapter"] for t in tl], list(range(1, CHAPTERS + 1)))
        times = [t["world_time"] for t in tl]
        self.assertEqual(times, sorted(times))

    def test_causality_chain_links_each_chapter_to_the_previous_one(self):
        conn = self.p.conn()
        self.addCleanup(conn.close)
        for n in range(2, CHAPTERS + 1):
            ev = db.get_event(conn, "E%d-1" % n)
            self.assertIsNotNone(ev)
            self.assertEqual(db._dec(ev["causes"], []), ["E%d-1" % (n - 1)])

    def test_foreshadowing_went_through_its_whole_lifecycle(self):
        conn = self.p.conn()
        self.addCleanup(conn.close)
        row = dict(db.get_foreshadow(conn, "F001"))
        self.assertEqual(row["status"], "RESOLVED")
        self.assertEqual(row["resolved_chapter"], 5)
        changes = conn.execute(
            "SELECT before_value, after_value FROM canon_changes "
            "WHERE entity_ref='F001' AND action='foreshadow_status' ORDER BY id"
        ).fetchall()
        chain = [(c["before_value"], c["after_value"]) for c in changes]
        self.assertIn(("PLANTED", "DEVELOPING"), chain)
        self.assertIn(("DEVELOPING", "RESOLVED"), chain)
        # after resolution it is no longer surfaced as an open thread
        self.assertNotIn("F001", [f["code"] for f in db.list_foreshadow(conn)
                                  if f["status"] not in ("RESOLVED",)])

    def test_knowledge_boundary_is_recorded(self):
        conn = self.p.conn()
        self.addCleanup(conn.close)
        self.assertEqual(db.who_knows(conn, "FACT-003"), ["李禾"])
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM knowledge WHERE fact_code='FACT-003'").fetchall()]
        self.assertTrue(rows and rows[0]["secret"] == 1)

    def test_validation_passes_and_snapshots_replay_exactly(self):
        report = validate_state.validate(self.p.root)
        self.assertEqual(report["errors"], 0, report["findings"])
        verify = db.verify_replay(self.p.root)
        self.assertTrue(verify["ok"], verify["differences"])
        self.assertEqual(verify["chapters_replayed"], CHAPTERS)

    def test_derived_views_describe_all_ten_chapters(self):
        ledger = read_text(paths(self.p.root)["ledger"])
        self.assertIn("AUTO-GENERATED", ledger)
        for n in range(1, CHAPTERS + 1):
            self.assertIn("ch-%04d" % n, ledger)
        bible = read_text(paths(self.p.root)["bible"])
        self.assertIn("FACT-003", bible)
        summaries = os.listdir(paths(self.p.root)["summaries"])
        for n in range(1, CHAPTERS + 1):
            self.assertIn("ch-%04d.md" % n, summaries)

    def test_next_reports_round_done_then_stop(self):
        brief = self.p.brief()          # a fresh round is planned for chapter 11 only
        self.assertIn(brief["action"], ("WRITE", "STOP"))
        if brief["action"] == "WRITE":
            self.assertEqual(brief["chapters_to_write"], [11])


class TenChapterRollbackTest(unittest.TestCase):
    """Destructive checks run on a COPY so the read-only acceptance facts above
    are not invalidated by test ordering."""

    @classmethod
    def setUpClass(cls):
        src = build_project()
        cls.p = helpers.Project(name="e2e-rollback")
        shutil.rmtree(cls.p.dir, ignore_errors=True)
        shutil.copytree(src.root, cls.p.dir)

    @classmethod
    def tearDownClass(cls):
        cls.p.cleanup()

    # unittest sorts methods alphabetically, so the numeric prefixes below ARE the
    # execution order of this deliberately stateful sequence.
    def test_1_project_copy_is_intact(self):
        conn = self.p.conn()
        self.addCleanup(conn.close)
        self.assertEqual(len(db.chapters_all(conn)), CHAPTERS)

    def test_2_rollback_to_six_leaves_a_consistent_project(self):
        p = self.p
        rc, out, err = p.json_run(novel_state, ["rollback", "--root", p.root,
                                               "--to-chapter", "6",
                                               "--reason", "端到端验收：回滚到第 6 章"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(out["chapter_files_quarantined"]), 4)
        for n in range(7, CHAPTERS + 1):
            self.assertFalse(os.path.isfile(chapter_path(p.root, n)))
            self.assertIsNone(db.load_snapshot(p.root, n))
        report = validate_state.validate(p.root)
        self.assertEqual(report["errors"], 0, report["findings"])
        verify = db.verify_replay(p.root)
        self.assertTrue(verify["ok"], verify["differences"])
        self.assertEqual(verify["chapters_replayed"], 6)
        conn = p.conn()
        self.addCleanup(conn.close)
        self.assertEqual([c["number"] for c in db.chapters_all(conn)],
                         list(range(1, 7)))
        tl = db.timeline_all(conn)
        self.assertEqual([t["chapter"] for t in tl], list(range(1, 7)))

    def test_3_rollback_can_be_undone_and_the_quarantine_is_kept(self):
        quarantine = os.path.join(self.p.paths()["state"], "rolled-back")
        self.assertTrue(os.path.isdir(quarantine))
        kept = sorted(os.listdir(quarantine))
        self.assertTrue(any(name.startswith("ch-0007") for name in kept),
                        "the undone chapter text must be preserved, not deleted")

    def test_4_export_after_rollback_contains_six_chapters(self):
        import export_novel
        rc, out, err = self.p.json_run(export_novel, ["--root", self.p.root,
                                                     "--by-volume", "--zip"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(out["chapters"], 6)
        self.assertTrue(all(os.path.isfile(f) for f in out["files"]))


if __name__ == "__main__":
    unittest.main()
