"""Character state machine, foreshadowing lifecycle, knowledge and timeline."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import helpers  # noqa: E402

import canon_db as db  # noqa: E402
import character_state as cs  # noqa: E402
import foreshadow as fs  # noqa: E402


class CharacterStateMachineTest(unittest.TestCase):
    def test_transition_applies_and_diffs(self):
        state = cs.empty_state("李禾")
        new, applied, warn = cs.apply_change(state, {
            "character": "李禾", "field": "location", "before": None, "after": "麦田"})
        self.assertIsNone(warn)
        self.assertEqual(applied["before"], None)
        self.assertEqual(new["location"], "麦田")
        diff = cs.diff_states(state, new)
        self.assertEqual(diff, [{"field": "location", "before": None, "after": "麦田"}])

    def test_unknown_field_is_rejected_with_a_warning(self):
        state = cs.empty_state("李禾")
        new, applied, warn = cs.apply_change(state, {"character": "李禾",
                                                    "field": "soul", "after": "x"})
        self.assertIsNone(applied)
        self.assertEqual(warn["code"], "STATE_TRANSITION_REJECTED")

    def test_missing_after_is_never_applied(self):
        state = cs.empty_state("李禾")
        new, applied, warn = cs.apply_change(state, {"character": "李禾",
                                                    "field": "emotion", "after": None})
        self.assertIsNone(applied)
        self.assertIn("after", warn["message"])

    def test_irreversible_status_needs_an_explicit_revival(self):
        state = cs.empty_state("李禾")
        state["status"] = "dead"
        new, applied, warn = cs.apply_change(state, {"character": "李禾",
                                                    "field": "status", "after": "alive"})
        self.assertIsNone(applied)
        self.assertIn("不可逆", warn["message"])

    def test_list_fields_merge_instead_of_replacing(self):
        state = cs.empty_state("李禾")
        state["knowledge"] = [{"fact_code": "FACT-001"}]
        new, _, _ = cs.apply_change(state, {
            "character": "李禾", "field": "knowledge", "after": [{"fact_code": "FACT-002"}]})
        codes = {k["fact_code"] for k in new["knowledge"]}
        self.assertEqual(codes, {"FACT-001", "FACT-002"})

    def test_knows_and_grant(self):
        state = cs.grant_knowledge(cs.empty_state("李禾"), "FACT-009", chapter=3)
        self.assertTrue(cs.knows(state, "FACT-009"))
        self.assertFalse(cs.knows(state, "FACT-010"))

    def test_validate_reports_shape_problems(self):
        issues = cs.validate({"knowledge": "not-a-list", "status": 7})
        self.assertTrue(any("knowledge" in i for i in issues))
        self.assertTrue(any("status" in i for i in issues))

    def test_validate_against_canon_flags_unknown_location(self):
        state = cs.empty_state("李禾")
        state["location"] = "不存在的地方"
        issues = cs.validate_against_canon(state, locations=["黑塔"], characters=["李禾"])
        self.assertTrue(any("不存在的地方" in i for i in issues))


class ForeshadowLifecycleTest(unittest.TestCase):
    def rec(self, status="PLANTED", start=4, end=6, **kw):
        d = {"code": "F001", "title": "铜钥匙", "status": status,
             "planned_payoff_start": start, "planned_payoff_end": end,
             "last_touched_chapter": 1, "planted_chapter": 1, "data": {}}
        d.update(kw)
        return d

    def test_legal_and_illegal_transitions(self):
        self.assertTrue(fs.validate_transition("PLANTED", "ACTIVE")[0])
        self.assertTrue(fs.validate_transition("PAYOFF_READY", "RESOLVED")[0])
        ok, reason = fs.validate_transition("RESOLVED", "ACTIVE")
        self.assertFalse(ok)
        self.assertIn("非法状态迁移", reason)
        ok, reason = fs.validate_transition("PLANTED", "NONSENSE")
        self.assertFalse(ok)

    def test_actions_drive_the_state(self):
        self.assertEqual(fs.transition_plan("PLANTED", "touch")[0], "ACTIVE")
        self.assertEqual(fs.transition_plan("ACTIVE", "develop")[0], "DEVELOPING")
        self.assertEqual(fs.transition_plan("DEVELOPING", "ready")[0], "PAYOFF_READY")
        self.assertEqual(fs.transition_plan("PAYOFF_READY", "resolve")[0], "RESOLVED")
        self.assertEqual(fs.transition_plan("ACTIVE", "leak")[0], "STALE_LEAK")

    def test_window_phase(self):
        self.assertEqual(fs.window_phase(self.rec(), 2), "before")
        self.assertEqual(fs.window_phase(self.rec(), 5), "inside")
        self.assertEqual(fs.window_phase(self.rec(), 9), "past")

    def test_overdue_after_the_window_plus_grace(self):
        self.assertFalse(fs.lifecycle_status(self.rec(), 7)["overdue"])
        self.assertTrue(fs.lifecycle_status(self.rec(), 12)["overdue"])

    def test_resolved_is_never_overdue_or_open(self):
        info = fs.lifecycle_status(self.rec(status="RESOLVED"), 50)
        self.assertFalse(info["overdue"])
        self.assertFalse(info["open"])

    def test_relevance_explains_itself(self):
        score, reasons = fs.relevance(self.rec(), 5, entities=["李禾"])
        self.assertGreater(score, 0)
        self.assertTrue(any("计划回收窗口" in r for r in reasons))

    def test_select_relevant_orders_by_score_and_marks_overdue(self):
        records = [self.rec(code="F001", start=50, end=60),
                   self.rec(code="F002", status="PAYOFF_READY", start=2, end=3),
                   self.rec(code="F003", status="RESOLVED", start=2, end=3)]
        picked = fs.select_relevant(records, 20)
        codes = [p["code"] for p in picked]
        self.assertIn("F002", codes)
        self.assertNotIn("F003", codes)
        f002 = [p for p in picked if p["code"] == "F002"][0]
        self.assertTrue(f002["overdue"])
        self.assertTrue(f002["reasons"])

    def test_detect_leaks_before_the_reveal_window(self):
        records = [self.rec(status="ACTIVE", start=40, end=50,
                            data={"keywords": ["种子库"]})]
        hits = fs.detect_leaks(records, "他提到了种子库这三个字。", 10)
        self.assertEqual(len(hits), 1)
        self.assertIn("种子库", hits[0]["keywords"])
        # inside the reveal window it is not a leak any more
        self.assertEqual(fs.detect_leaks(records, "种子库", 45), [])

    def test_illegal_transition_is_rejected_by_plan_helper(self):
        after, ok, reason = fs.transition_plan("PAYOFF_READY", "plant")
        self.assertFalse(ok)
        self.assertEqual(after, "PLANTED")
        # terminal states are not silently resurrected
        after, ok, _ = fs.transition_plan("RESOLVED", "develop")
        self.assertTrue(ok)
        self.assertEqual(after, "RESOLVED")


class CanonStateTest(unittest.TestCase):
    def setUp(self):
        self.p = helpers.fresh_project(name="state")
        self.p.full_chapter(1)
        self.p.full_chapter(2)
        self.conn = self.p.conn()
        self.addCleanup(self.conn.close)

    def test_knowledge_is_recorded_and_queryable(self):
        db.add_knowledge(self.conn, {"knower": "李禾", "fact_code": "FACT-003",
                                     "fact_text": "账本第二页是空白的", "secret": True},
                         chapter=2)
        self.assertEqual(db.who_knows(self.conn, "FACT-003"), ["李禾"])
        self.assertEqual(db.who_knows(self.conn, "FACT-404"), [])

    def test_timeline_is_recorded_per_chapter(self):
        tl = db.timeline_all(self.conn)
        self.assertEqual([t["chapter"] for t in tl], [1, 2])
        self.assertLess(tl[0]["world_time"], tl[1]["world_time"])

    def test_events_carry_causality(self):
        ev2 = db.get_event(self.conn, "E2-1")
        self.assertEqual(db._dec(ev2["causes"], []), ["E1-1"])
        self.assertEqual(db._dec(ev2["participants"], []), ["李禾", "沈默"])

    def test_items_track_custody(self):
        db.upsert_item(self.conn, {"code": "I01", "name": "铜钥匙", "holder": "李禾",
                                   "location": "黑塔"}, chapter=2)
        items = db.list_items(self.conn)
        self.assertEqual(items[0]["holder_name"], "李禾")

    def test_relationships_are_queryable_from_both_sides(self):
        db.upsert_relationship(self.conn, {"from": "李禾", "to": "沈默",
                                           "kind": "trust", "status": "strained"},
                               chapter=2)
        self.assertEqual(len(db.list_relationships(self.conn, "李禾")), 1)
        self.assertEqual(len(db.list_relationships(self.conn, "沈默")), 1)

    def test_world_rule_immutability_blocks_silent_rewrite(self):
        before = self.conn.execute("SELECT text FROM world_rules WHERE code='R1'"
                                   ).fetchone()["text"]
        db.upsert_world_rule(self.conn, {"code": "R1", "text": "完全不同的规则"},
                             chapter=2)
        after = self.conn.execute("SELECT text FROM world_rules WHERE code='R1'"
                                  ).fetchone()["text"]
        self.assertEqual(before, after, "an immutable rule must not be silently rewritten")
        warn = self.conn.execute("SELECT * FROM warnings WHERE code='IMMUTABLE_RULE_CONFLICT'"
                                 ).fetchone()
        self.assertIsNotNone(warn)

    def test_scene_index_supports_semantic_repetition_checks(self):
        scenes = db.list_scenes(self.conn)
        self.assertEqual(len(scenes), 2)
        self.assertTrue(all(s["participants"] for s in scenes))


if __name__ == "__main__":
    unittest.main()
