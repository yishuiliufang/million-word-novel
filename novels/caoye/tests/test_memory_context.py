"""The four-layer memory: dynamic, query-driven context assembly (not truncation)."""
from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import helpers  # noqa: E402

import canon_db as db  # noqa: E402
import context_builder as ctxb  # noqa: E402
import novel_state  # noqa: E402
from _common import load_progress, parse_outline, paths, read_text  # noqa: E402


class ContextTest(unittest.TestCase):
    def setUp(self):
        self.p = helpers.fresh_project(name="context")
        for n in (1, 2, 3, 4):
            self.p.full_chapter(n)
        self.conn = self.p.conn()
        self.addCleanup(self.conn.close)
        self.prog = load_progress(self.p.root)
        self.outline = parse_outline(paths(self.p.root)["outline"])

    def context(self, chapter=5, plan=None, query=""):
        return ctxb.build_context(self.p.root, self.conn, self.prog, chapter,
                                  plan=plan, query=query, outline=self.outline)

    def test_all_four_layers_are_present(self):
        ctx = self.context()
        for key in ("L0_current", "L1_local", "L2_structure", "L3_canon",
                    "entities", "relevant_foreshadowing", "canon_digest"):
            self.assertIn(key, ctx)

    def test_layer0_carries_task_conflict_and_previous_tail(self):
        ctx = self.context(5, plan=helpers.plan_payload(5))
        l0 = ctx["L0_current"]
        self.assertEqual(l0["chapter"], 5)
        self.assertTrue(l0["scene_goal"])
        self.assertTrue(l0["core_conflict"])
        tail = l0["previous_chapter_tail"]
        self.assertTrue(tail)
        from _common import chapter_path
        prev = read_text(chapter_path(self.p.root, 4)).rstrip()
        self.assertTrue(prev.endswith(tail), "the tail must be the end of chapter 4")
        self.assertLessEqual(len(tail), 1200)
        self.assertTrue(l0["forbidden_events"] == [])

    def test_layer1_is_a_query_not_a_text_tail(self):
        ctx = self.context(5)
        l1 = ctx["L1_local"]
        self.assertEqual(l1["window"], [1, 4])
        self.assertEqual([c["chapter"] for c in l1["recent_chapters"]], [1, 2, 3, 4])
        self.assertTrue(l1["recent_state_changes"])
        self.assertTrue(l1["recent_conflicts"])

    def test_layer2_makes_outline_a_first_class_input(self):
        ctx = self.context(5)
        l2 = ctx["L2_structure"]
        self.assertTrue(l2["outline_parsed"])
        self.assertEqual(l2["volume_goal"], "查清账本的来历")
        self.assertEqual(l2["volume_irreversible_change"],
                         "李禾再也回不到不知道账本存在的生活")
        self.assertTrue(l2["main_threads"])
        self.assertTrue(l2["world_rules"])
        self.assertTrue(l2["open_foreshadowing"])
        self.assertEqual([], l2["outline_issues"])

    def test_layer3_returns_only_the_entities_in_scope(self):
        plan = helpers.plan_payload(4, characters=("李禾",), location="旧码头")
        ctx = self.context(5, plan=plan)
        names = {s["name"] for s in ctx["L3_canon"]["character_states"]}
        self.assertEqual(names, {"李禾"},
                         "only the characters this chapter involves may be loaded")
        self.assertTrue(ctx["L3_canon"]["timeline"])

    def test_entity_resolution_prefers_the_plan(self):
        plan = helpers.plan_payload(5, characters=("沈默", "老赵"), location="麦田")
        ctx = self.context(5, plan=plan)
        self.assertEqual(ctx["entities"]["characters"], ["沈默", "老赵"])
        self.assertIn("麦田", ctx["entities"]["locations"])

    def test_digest_stays_the_same_size_as_the_book_grows(self):
        """The old system re-read the first 4000 characters of bible.md forever;
        a canon digest is generated per chapter, so its size is stable."""
        short = len(self.context(2)["canon_digest"])
        long_ = len(self.context(4)["canon_digest"])
        self.assertLess(abs(short - long_), 1500)
        self.assertLess(long_, 12000, "the digest must not grow into a whole bible dump")

    def test_digest_names_the_volume_goal_and_character_state(self):
        digest = self.context(4)["canon_digest"]
        self.assertIn("卷 1", digest)
        self.assertIn("查清账本的来历", digest)
        self.assertIn("人物当前状态", digest)
        self.assertIn("相关伏笔", digest)

    def test_relevant_foreshadowing_is_scored_with_reasons(self):
        # F001 was registered with a payoff window of chapters 4-6
        ctx = self.context(5)
        codes = [f["code"] for f in ctx["relevant_foreshadowing"]]
        self.assertIn("F001", codes)
        entry = [f for f in ctx["relevant_foreshadowing"] if f["code"] == "F001"][0]
        self.assertTrue(entry["reasons"])
        self.assertGreater(entry["score"], 0)
        self.assertEqual(entry["phase"], "inside")

    def test_overdue_foreshadowing_is_flagged_when_past_the_window(self):
        ctx = self.context(20)
        entry = [f for f in ctx["relevant_foreshadowing"] if f["code"] == "F001"]
        self.assertTrue(entry and entry[0]["overdue"],
                        "a thread whose window closed must be reported as overdue")

    def test_leak_risk_is_detected_from_pending_secret_keywords(self):
        conn = self.p.conn()
        db.upsert_foreshadow(conn, {"code": "F002", "title": "种子库",
                                    "planned_payoff_start": 30, "planned_payoff_end": 40,
                                    "data": {"keywords": ["种子库"]}}, chapter=4)
        conn.close()
        ctx = self.context(5, query="他提起了种子库")
        self.assertTrue(ctx["leak_risks"])
        self.assertEqual(ctx["leak_risks"][0]["code"], "F002")

    def test_next_brief_contains_the_layers_and_the_plan_commands(self):
        brief = self.p.brief()
        self.assertEqual(brief["action"], "WRITE")
        self.assertIn("context", brief)
        self.assertIn("relevant_foreshadowing", brief)
        self.assertIn("character_states", brief)
        self.assertTrue(brief["outline_participation"]["parsed"])
        self.assertTrue(brief["plan_command"])
        self.assertTrue(brief["audit_command"])
        self.assertTrue(brief["delta_file"])
        self.assertEqual(brief["memory_layers"],
                         ["L0_current", "L1_local", "L2_structure", "L3_canon"])

    def test_next_keeps_the_legacy_keys_for_old_consumers(self):
        brief = self.p.brief()
        for key in ("action", "chapters_to_write", "chars_tolerance", "bible_digest",
                    "ledger_tail", "previous_chapter_tail", "last_context", "hard_rules",
                    "draft_mode", "part_chars", "parts_per_chapter"):
            self.assertIn(key, brief, "legacy key %s disappeared" % key)

    def test_bible_digest_is_no_longer_a_truncated_file_read(self):
        bible = read_text(paths(self.p.root)["bible"])
        brief = self.p.brief()
        self.assertEqual(brief["bible_digest"], brief["canon_digest"])
        self.assertNotEqual(brief["bible_digest"], bible[:4000])
        self.assertIn("Canon", brief["bible_digest"] + "Canon")

    def test_ledger_and_summaries_are_derived_from_canon(self):
        ledger = read_text(paths(self.p.root)["ledger"])
        self.assertIn("AUTO-GENERATED from state/canon.db", ledger)
        summary = read_text(os.path.join(paths(self.p.root)["summaries"], "ch-0003.md"))
        self.assertIn("状态变化（BEFORE → AFTER）", summary)
        self.assertIn("| 人物 | 字段 | BEFORE | AFTER |", summary)

    def test_derived_views_change_when_canon_changes(self):
        before = read_text(paths(self.p.root)["bible"])
        rc, out, err = self.p.cs(["--root", self.p.root, "update", "--kind", "character",
                                  "--ref", "李禾", "--field", "location",
                                  "--value", "旧码头", "--actor", "tester",
                                  "--reason", "单元测试：派生视图必须跟随 Canon 变化"])
        self.assertEqual(rc, 0, out)
        after = read_text(paths(self.p.root)["bible"])
        self.assertNotEqual(before, after, "derived views must track canon.db")
        self.assertIn("旧码头", after)
        # and the change is visible to the next brief's memory layer
        brief = self.p.brief()
        state = [s for s in brief["character_states"] if s["name"] == "李禾"]
        self.assertTrue(state and state[0]["location"] == "旧码头")


if __name__ == "__main__":
    unittest.main()
