"""Core infrastructure: schema, entity/state storage, snapshots, replay,
transactional commit, crash recovery and rollback."""
from __future__ import annotations

import json
import os
import shutil
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import helpers  # noqa: E402
from helpers import Project, fresh_project  # noqa: E402

import canon_db as db  # noqa: E402
import novel_state  # noqa: E402
import tx as tx_mod  # noqa: E402
import validate_state  # noqa: E402
from _common import read_text, sha256_text, canonical_text  # noqa: E402


class SchemaTest(unittest.TestCase):
    def setUp(self):
        self.p = fresh_project(name="schema")
        self.addCleanup(self.p.cleanup)

    def test_ddl_file_is_the_source_of_the_schema(self):
        ddl = db.ddl()
        for table in ("meta", "chapters", "entities", "character_states",
                      "character_state_changes", "events", "relationships",
                      "foreshadowings", "plot_threads", "world_rules", "knowledge",
                      "items", "volumes", "arcs", "timeline", "snapshots", "plans",
                      "audits", "gates", "transactions", "round_runs", "round_tasks",
                      "canon_changes", "warnings", "scene_index"):
            self.assertIn("CREATE TABLE IF NOT EXISTS %s" % table, ddl,
                          "DDL is missing table %s" % table)

    def test_required_tables_and_indexes_exist(self):
        conn = self.p.conn()
        self.addCleanup(conn.close)
        names = set(db.table_names(conn))
        for t in ("entities", "character_states", "events", "foreshadowings",
                  "plot_threads", "world_rules", "knowledge", "snapshots", "plans",
                  "audits", "canon_changes", "warnings", "round_runs"):
            self.assertIn(t, names)
        report = {r["table"]: r for r in db.schema_report(conn)}
        self.assertTrue(any(i["name"] == "idx_entities_kind"
                            for i in report["entities"]["indexes"]))
        self.assertTrue(any(i["name"] == "idx_snapshots_chapter"
                            for i in report["snapshots"]["indexes"]))
        # every table declares at least a primary key column
        for t, info in report.items():
            self.assertTrue(any(c["pk"] for c in info["columns"]),
                            "%s has no primary key" % t)

    def test_json_columns_round_trip(self):
        conn = self.p.conn()
        self.addCleanup(conn.close)
        db.ensure_entity(conn, "character", "李禾", aliases=["禾禾"], chapter=1)
        row = db.get_entity(conn, "character", "禾禾")
        self.assertIsNotNone(row, "alias lookup must resolve to the canonical row")
        self.assertEqual(row["name"], "李禾")
        db.upsert_world_rule(conn, {"code": "R1", "text": "账本即现实",
                                    "immutable": True}, chapter=1)
        rules = [dict(r) for r in conn.execute("SELECT * FROM world_rules").fetchall()]
        self.assertEqual(rules[0]["code"], "R1")


class EntityAndStateTest(unittest.TestCase):
    def setUp(self):
        self.p = fresh_project(name="entity")
        self.p.full_chapter(1)
        self.conn = self.p.conn()
        self.addCleanup(self.conn.close)

    def test_character_state_transition_is_logged_before_and_after(self):
        before = db.get_character_state(self.conn, "李禾")
        self.assertEqual(before["goal"], "第1章的目标")
        db.set_character_field(self.conn, "李禾", "location", "麦田", chapter=2)
        after = db.get_character_state(self.conn, "李禾")
        self.assertEqual(after["location"], "麦田")
        history = db.character_state_history(self.conn, "李禾")
        loc_rows = [h for h in history if h["field"] == "location"]
        self.assertEqual(len(loc_rows), 1)
        self.assertIsNone(loc_rows[0]["before"])
        self.assertEqual(loc_rows[0]["after"], "麦田")

    def test_unknown_field_is_rejected(self):
        with self.assertRaises(Exception):
            db.set_character_field(self.conn, "李禾", "soul", "x", chapter=2)

    def test_none_after_never_rewrites_canon_and_records_warning(self):
        db.set_character_field(self.conn, "李禾", "emotion", None, chapter=3)
        state = db.get_character_state(self.conn, "李禾")
        self.assertIsNone(state["emotion"], "an unknown value must not overwrite canon")
        warns = [dict(r) for r in self.conn.execute(
            "SELECT * FROM warnings WHERE code='UNCERTAIN_STATE_CHANGE'").fetchall()]
        self.assertTrue(warns, "uncertainty must be recorded as a warning")

    def test_change_log_records_actor_and_reason(self):
        db.log_change(self.conn, "test_action", actor="tester", entity_kind="character",
                      entity_ref="李禾", field="goal", before="a", after="b",
                      reason="unit test")
        row = self.conn.execute("SELECT * FROM canon_changes WHERE action='test_action'"
                                ).fetchone()
        self.assertEqual(row["actor"], "tester")
        self.assertEqual(row["reason"], "unit test")


class SnapshotTest(unittest.TestCase):
    def setUp(self):
        self.p = fresh_project(name="snapshot")
        for n in (1, 2, 3):
            self.p.full_chapter(n)
        self.addCleanup(self.p.cleanup)

    def test_every_commit_produces_a_snapshot_file_and_row(self):
        for n in (1, 2, 3):
            payload = db.load_snapshot(self.p.root, n)
            self.assertIsNotNone(payload, "ch-%d snapshot missing" % n)
            for key in ("before", "events", "after", "character_changes",
                        "chapter_sha256", "timeline"):
                self.assertIn(key, payload)
            self.assertTrue(payload["events"], "BEFORE->EVENTS->AFTER requires events")
        conn = self.p.conn()
        self.addCleanup(conn.close)
        rows = conn.execute("SELECT chapter, kind FROM snapshots WHERE kind='delta' "
                            "ORDER BY chapter").fetchall()
        self.assertEqual([r["chapter"] for r in rows], [1, 2, 3])
        self.assertTrue(all(r["kind"] == "delta" for r in rows))
        # the bible import is itself recorded as a replayable manual patch
        manual = conn.execute("SELECT COUNT(*) c FROM snapshots WHERE kind='manual'"
                              ).fetchone()["c"]
        self.assertGreaterEqual(manual, 1)

    def test_snapshot_before_after_is_a_real_diff(self):
        payload = db.load_snapshot(self.p.root, 2)
        changes = payload["character_changes"]
        self.assertTrue(changes)
        self.assertIn("character", changes[0])
        self.assertIn("field", changes[0])
        self.assertIn("after", changes[0])

    def test_replay_reproduces_live_canon_exactly(self):
        report = db.verify_replay(self.p.root)
        self.assertTrue(report["ok"], "replay diverged: %s" % report["differences"])
        self.assertEqual(report["chapters_replayed"], 3)

    def test_compare_two_snapshots(self):
        a = db.load_snapshot(self.p.root, 1)
        c = db.load_snapshot(self.p.root, 3)
        diff = db.diff_snapshots(a, c)
        self.assertEqual(diff["from_chapter"], 1)
        self.assertEqual(diff["to_chapter"], 3)

    def test_chapter_hash_matches_file(self):
        conn = self.p.conn()
        self.addCleanup(conn.close)
        row = db.chapter_row(conn, 2)
        from _common import chapter_path
        text = canonical_text(read_text(chapter_path(self.p.root, 2)))
        self.assertEqual(row["sha256"], sha256_text(text))


class TransactionTest(unittest.TestCase):
    def setUp(self):
        self.p = fresh_project(name="tx")
        self.p.full_chapter(1)
        self.addCleanup(self.p.cleanup)

    def test_successful_commit_leaves_no_journal(self):
        journals = [d for d in os.listdir(self.p.paths()["tx"])] if os.path.isdir(
            self.p.paths()["tx"]) else []
        self.assertEqual(journals, [], "a finished commit must clean its staging dir")

    def test_interrupted_publish_is_completed_by_recover(self):
        """Simulate a crash after the DB committed but before files were published."""
        conn = self.p.conn()
        txn = tx_mod.CommitTransaction(self.p.root, 2)
        txn.write_text("chapter", os.path.join(self.p.paths()["chapters"],
                                               "ch-0002.txt"), "第二稿正文\n")
        txn.mark_db_committed()
        staged_exists = os.path.isfile(txn.files[0]["staged"])
        self.assertTrue(staged_exists)
        target = txn.files[0]["final"]
        self.assertFalse(os.path.isfile(target), "publish must not have happened yet")

        result = tx_mod.recover(self.p.root, conn)
        conn.close()
        self.assertTrue(result["found"], "the journal must be detected")
        self.assertTrue(os.path.isfile(target), "recover must complete publication")
        self.assertEqual(read_text(target), "第二稿正文\n")
        self.assertEqual([d for d in os.listdir(self.p.paths()["tx"])], [])

    def test_interrupted_prepare_is_discarded(self):
        conn = self.p.conn()
        txn = tx_mod.CommitTransaction(self.p.root, 3)
        txn.write_text("chapter", os.path.join(self.p.paths()["chapters"],
                                               "ch-0003.txt"), "不该出现的正文\n")
        result = tx_mod.recover(self.p.root, conn)
        conn.close()
        self.assertEqual(result["actions"][0]["action"], "discard_staging")
        self.assertFalse(os.path.isfile(os.path.join(self.p.paths()["chapters"],
                                                     "ch-0003.txt")))

    def test_recover_is_idempotent(self):
        conn = self.p.conn()
        txn = tx_mod.CommitTransaction(self.p.root, 4)
        txn.write_text("chapter", os.path.join(self.p.paths()["chapters"],
                                               "ch-0004.txt"), "x\n")
        first = tx_mod.recover(self.p.root, conn)
        second = tx_mod.recover(self.p.root, conn)
        conn.close()
        self.assertTrue(first["found"])
        self.assertTrue(second["clean"])

    def test_pending_work_reports_uncommitted_draft(self):
        from _common import atomic_write_text, tolerance
        lo, hi = tolerance(self.p.progress())
        draft = os.path.join(self.p.paths()["drafts"], "ch-0099.md")
        atomic_write_text(draft, helpers.chapter_text(99, lo, hi))
        conn = self.p.conn()
        work = tx_mod.pending_work(self.p.root, conn)
        conn.close()
        self.assertEqual([d["chapter"] for d in work["uncommitted_drafts"]], [99])
        self.assertIn("未提交草稿", work["resume"])


class RollbackTest(unittest.TestCase):
    def setUp(self):
        self.p = fresh_project(name="rollback")
        for n in range(1, 5):
            self.p.full_chapter(n)
        self.addCleanup(self.p.cleanup)

    def test_rollback_restores_canon_and_quarantines_later_chapters(self):
        rc, out, err = self.p.json_run(novel_state, ["rollback", "--root", self.p.root,
                                                     "--to-chapter", "2",
                                                     "--reason", "unit test"])
        self.assertEqual(rc, 0, out)
        self.assertEqual(out["to_chapter"], 2)
        self.assertEqual(len(out["chapter_files_quarantined"]), 2)
        conn = self.p.conn()
        self.addCleanup(conn.close)
        self.assertEqual([c["number"] for c in db.chapters_all(conn)], [1, 2])
        for n in (3, 4):
            self.assertFalse(os.path.isfile(
                os.path.join(self.p.paths()["chapters"], "ch-%04d.txt" % n)),
                "ch-%d file must be gone from chapters/" % n)
            self.assertIsNone(db.load_snapshot(self.p.root, n))
            self.assertTrue(os.path.isfile(os.path.join(self.p.paths()["state"],
                                                        "rolled-back", "ch-%04d.txt" % n)))
        self.assertTrue(os.path.isfile(out["backup_db"]))

    def test_project_is_consistent_after_rollback(self):
        self.p.json_run(novel_state, ["rollback", "--root", self.p.root, "--to-chapter", "1"])
        report = validate_state.validate(self.p.root)
        self.assertEqual(report["errors"], 0, report["findings"])
        conn = self.p.conn()
        self.addCleanup(conn.close)
        self.assertTrue(db.verify_replay(self.p.root)["ok"])
        self.assertEqual(len(db.chapters_all(conn)), 1)

    def test_rollback_by_chapter_keeps_previous(self):
        rc, out, err = self.p.json_run(novel_state, ["rollback", "--root", self.p.root,
                                                     "--chapter", "4"])
        self.assertEqual(rc, 0, out)
        self.assertEqual(out["to_chapter"], 3)

    def test_manual_canon_update_survives_rollback_and_is_replayable(self):
        rc, out, err = self.p.cs(["--root", self.p.root, "update", "--kind", "character",
                                  "--ref", "李禾", "--field", "location",
                                  "--value", "旧码头", "--actor", "tester",
                                  "--reason", "作者确认李禾在第四章已到旧码头",
                                  "--chapter", "4"])
        self.assertEqual(rc, 0, out)
        conn = self.p.conn()
        self.assertEqual(db.get_character_state(conn, "李禾")["location"], "旧码头")
        conn.close()
        # keep chapter 4: the manual correction must survive the rollback rebuild
        self.p.json_run(novel_state, ["rollback", "--root", self.p.root, "--to-chapter", "4"])
        conn2 = self.p.conn()
        self.assertEqual(db.get_character_state(conn2, "李禾")["location"], "旧码头")
        conn2.close()
        self.assertTrue(db.verify_replay(self.p.root)["ok"], "manual patches must replay")
        # keep chapter 3: the correction (made at chapter 4) must be gone
        self.p.json_run(novel_state, ["rollback", "--root", self.p.root, "--to-chapter", "3"])
        conn3 = self.p.conn()
        self.assertNotEqual(db.get_character_state(conn3, "李禾")["location"], "旧码头")
        conn3.close()
        self.assertTrue(db.verify_replay(self.p.root)["ok"])


if __name__ == "__main__":
    unittest.main()
