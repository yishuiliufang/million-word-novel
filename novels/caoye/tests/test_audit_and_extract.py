"""Level 1 text hygiene, Level 2 semantic consistency, and Canon Delta validation."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import helpers  # noqa: E402

import canon_db as db  # noqa: E402
import extract as ex  # noqa: E402
import llm as llm_mod  # noqa: E402
import narrative_audit as na  # noqa: E402


def body(lines=40):
    """Varied prose so the detector sees natural repetition, not a template."""
    return "\n".join(helpers.line_for(7, i, "黑塔", i // 4) for i in range(lines)) + "\n"


class Level1Test(unittest.TestCase):
    def audit(self, text, **kw):
        from _common import count_chars
        chars = count_chars(text)
        opts = {"mode": "no_ws", "lo": max(1, chars - 10), "hi": chars + 10, "banned": ()}
        opts.update(kw)
        return na.level1(text, **opts)

    def test_short_chapter_blocks(self):
        r = self.audit("短。", lo=500)
        self.assertFalse(r["pass"])
        self.assertIn("LENGTH_SHORT", [b["code"] for b in r["blocking"]])

    def test_banned_phrase_blocks(self):
        text = body(20) + "他瞳孔骤缩。\n" + '"你来了。"他说。'
        r = self.audit(text, banned=["瞳孔骤缩"])
        self.assertIn("BANNED_PHRASE", [b["code"] for b in r["blocking"]])

    def test_no_dialogue_blocks(self):
        text = "\n".join(helpers.line_for(7, i * 4, "黑塔") for i in range(30)) + "\n"
        r = self.audit(text)
        self.assertIn("NO_DIALOGUE", [b["code"] for b in r["blocking"]])

    def test_overlong_paragraph_blocks(self):
        long_line = "他把账本翻到新的一页，灯芯短了一截，" * 6
        r = self.audit("\n".join([long_line] * 20) + '\n"你好。"他说。\n')
        self.assertIn("PARAGRAPH_TOO_LONG", [b["code"] for b in r["blocking"]])

    def test_ngram_repetition_is_advisory_not_a_verdict(self):
        """The brief says n-gram checks may only support, never decide."""
        phrase = "他把铜钥匙插进锁孔里试了三次"
        text = "\n".join([phrase + "。"] * 3) + "\n" + body(30)
        r = self.audit(text)
        codes = [b["code"] for b in r["blocking"]]
        self.assertNotIn("NGROV_REPETITION_EXTREME", codes, r["blocking"])
        self.assertIn("NGRAM_REPETITION", [a["code"] for a in r["advisories"]])
        self.assertTrue(r["pass"], "a repeated phrase alone must not fail a chapter")

    def test_extreme_repetition_still_blocks(self):
        phrase = "他把铜钥匙插进锁孔里试了三次"
        text = "\n".join([phrase + "。"] * 12) + "\n" + body(30)
        r = self.audit(text)
        self.assertIn("NGROV_REPETITION_EXTREME", [b["code"] for b in r["blocking"]])

    def test_prev_chapter_overlap_is_advisory(self):
        text = body(30)
        r = self.audit(text, prev_text=text)
        self.assertIn("PREV_OVERLAP", [a["code"] for a in r["advisories"]])
        self.assertTrue(r["pass"], r["blocking"])


class Level2Test(unittest.TestCase):
    def setUp(self):
        self.p = helpers.fresh_project(name="level2")
        self.p.full_chapter(1)
        self.conn = self.p.conn()
        self.addCleanup(self.conn.close)
        import context_builder as ctxb
        from _common import load_progress, parse_outline, paths
        self.canon = ctxb.canon_view(self.conn, load_progress(self.p.root),
                                     parse_outline(paths(self.p.root)["outline"]))

    def l2(self, text, delta=None, plan=None, chapter=2):
        return na.level2(text, chapter=chapter, conn=self.conn,
                         delta=delta if delta is not None else self._delta(chapter),
                         plan=plan, canon=self.canon)

    def _delta(self, chapter=2, **over):
        d = helpers.delta_payload(chapter)
        d.update(over)
        return d

    def test_timeline_regression_blocks(self):
        delta = self._delta(2, timeline={"world_time": "2000-01-01"})
        r = self.l2("正文。", delta)
        self.assertIn("TIMELINE_REGRESSION", [b["code"] for b in r["blocking"]])

    def test_dangling_cause_blocks(self):
        delta = self._delta(2)
        delta["events"][0]["causes"] = ["E999-9"]
        r = self.l2("正文。", delta)
        self.assertIn("DANGLING_CAUSE", [b["code"] for b in r["blocking"]])

    def test_forbidden_reveal_blocks(self):
        plan = helpers.plan_payload(2, forbidden=["FACT-SECRET"])
        r = self.l2("他忽然说出了 FACT-SECRET 这个词。", self._delta(2), plan=plan)
        self.assertIn("FORBIDDEN_REVEAL", [b["code"] for b in r["blocking"]])

    def test_dead_character_cannot_act(self):
        db.set_character_field(self.conn, "李禾", "status", "dead", chapter=1)
        r = self.l2("正文。", self._delta(2))
        self.assertIn("DEAD_CHARACTER_ACTIVE", [b["code"] for b in r["blocking"]])

    def test_planned_state_change_missing_blocks(self):
        plan = helpers.plan_payload(2, must_change=[
            {"character": "李禾", "field": "health", "before": None, "after": "受伤"}])
        r = self.l2("正文。", self._delta(2), plan=plan)
        self.assertIn("PLANNED_STATE_CHANGE_MISSING",
                      [b["code"] for b in r["blocking"]])

    def test_planned_state_change_divergence_blocks(self):
        plan = helpers.plan_payload(2, must_change=[
            {"character": "李禾", "field": "goal", "before": None, "after": "另一个目标"}])
        r = self.l2("正文。", self._delta(2), plan=plan)
        self.assertIn("PLANNED_STATE_CHANGE_DIVERGED",
                      [b["code"] for b in r["blocking"]])

    def test_semantic_plot_repetition_is_reported(self):
        """Two chapters can share zero 8-grams and still be the same scene."""
        delta = self._delta(2)
        delta["scenes"] = [helpers.scene_payload(1, helpers.LOCATIONS[0], ("李禾", "沈默"))]
        delta["events"] = []
        r = self.l2("正文。", delta)
        codes = [a["code"] for a in r["advisories"]]
        self.assertIn("POSSIBLE_PLOT_REPETITION", codes)
        self.assertTrue(r["pass"], "plot repetition is a prompt to a human, not a blocker")

    def test_unknown_speaker_is_reported(self):
        r = self.l2('"我们走吧。"陌生人甲说。\n')
        self.assertIn("UNKNOWN_SPEAKER", [a["code"] for a in r["advisories"]])

    def test_knowledge_boundary_flags_secret_nobody_knows(self):
        self.conn.execute("INSERT INTO knowledge(knower_name,fact_code,fact_text,secret,"
                          "learned_chapter) VALUES('','FACT-777','第七页是空白的',1,1)")
        r = self.l2("他说出了第七页是空白的这件事。")
        self.assertIn("KNOWLEDGE_BOUNDARY", [a["code"] for a in r["advisories"]])

    def test_illegal_foreshadow_transition_blocks(self):
        db.upsert_foreshadow(self.conn, {"code": "F001", "status": "RESOLVED",
                                         "planned_payoff_start": 4,
                                         "planned_payoff_end": 6}, chapter=1)
        delta = self._delta(2, foreshadowing=[{"code": "F001", "status": "ACTIVE",
                                               "action": "advance"}])
        r = self.l2("正文。", delta)
        self.assertIn("ILLEGAL_FORESHADOW_TRANSITION",
                      [b["code"] for b in r["blocking"]])


class DeltaValidationTest(unittest.TestCase):
    def setUp(self):
        self.p = helpers.fresh_project(name="delta")
        self.p.full_chapter(1)
        self.conn = self.p.conn()
        self.addCleanup(self.conn.close)
        import context_builder as ctxb
        import chapter_plan as cp
        from _common import load_progress, parse_outline, paths
        self.canon = ctxb.canon_view(self.conn, load_progress(self.p.root),
                                     parse_outline(paths(self.p.root)["outline"]))

    def test_valid_delta_passes(self):
        v = ex.validate_delta(helpers.delta_payload(2), chapter=2, canon=self.canon)
        self.assertTrue(v["ok"], v["errors"])

    def test_chapter_mismatch_is_an_error(self):
        v = ex.validate_delta(helpers.delta_payload(3), chapter=2, canon=self.canon)
        self.assertFalse(v["ok"])
        self.assertTrue(any("chapter" in e for e in v["errors"]))

    def test_missing_required_field_is_an_error(self):
        d = helpers.delta_payload(2)
        d["events"] = [{"summary": ""}]
        v = ex.validate_delta(d, chapter=2, canon=self.canon)
        self.assertFalse(v["ok"])

    def test_illegal_foreshadow_status_is_an_error(self):
        d = helpers.delta_payload(2, foreshadowing=[{"code": "F001", "status": "MAYBE"}])
        v = ex.validate_delta(d, chapter=2, canon=self.canon)
        self.assertFalse(v["ok"])
        self.assertTrue(any("非法状态" in e for e in v["errors"]))

    def test_inverted_payoff_window_is_an_error(self):
        d = helpers.delta_payload(2, foreshadowing=[
            {"code": "F001", "action": "plant", "planned_payoff_start": 40,
             "planned_payoff_end": 10}])
        v = ex.validate_delta(d, chapter=2, canon=self.canon)
        self.assertFalse(v["ok"])

    def test_timeline_regression_is_an_error(self):
        d = helpers.delta_payload(2, )
        d["timeline"] = {"world_time": "1999-01-01"}
        v = ex.validate_delta(d, chapter=2, canon=self.canon)
        self.assertFalse(v["ok"])
        self.assertTrue(any("倒退" in e for e in v["errors"]))

    def test_uncertain_items_never_reach_canon(self):
        d = helpers.delta_payload(2)
        d["events"].append({"summary": "可能是幻觉", "uncertain": True})
        d["character_changes"].append({"character": "李禾", "field": "emotion",
                                       "after": "愤怒", "confidence": 0.2})
        v = ex.validate_delta(d, chapter=2, canon=self.canon)
        self.assertTrue(v["ok"], v["errors"])
        self.assertEqual(len(v["delta"]["events"]), 1, "the uncertain event must be dropped")
        self.assertEqual(len(v["delta"]["character_changes"]), 1)
        codes = [w["code"] for w in v["warnings"]]
        self.assertEqual(codes.count("UNCERTAIN_ITEM_DROPPED"), 2)

    def test_ok_false_flag_from_ledger_window(self):
        d = helpers.delta_payload(2, foreshadowing=[{"code": "F001", "action": "plant"}])
        v = ex.validate_delta(d, chapter=2, canon=self.canon)
        self.assertTrue(any(w["code"] == "FORESHADOW_NO_WINDOW" for w in v["warnings"]))

    def test_unknown_named_speakers_are_warned_not_errored(self):
        d = helpers.delta_payload(2)
        d["events"][0]["participants"] = ["李禾", "沈默", "从未出现过的人"]
        v = ex.validate_delta(d, chapter=2, canon=self.canon)
        self.assertTrue(v["ok"])
        self.assertTrue(any(w["code"] == "IMPLICIT_ENTITY" for w in v["warnings"]))


class RuleExtractorTest(unittest.TestCase):
    def setUp(self):
        self.p = helpers.fresh_project(name="ruleextract")
        self.p.full_chapter(1)
        self.conn = self.p.conn()
        self.addCleanup(self.conn.close)
        import context_builder as ctxb
        from _common import load_progress, parse_outline, paths
        self.canon = ctxb.canon_view(self.conn, load_progress(self.p.root),
                                     parse_outline(paths(self.p.root)["outline"]))
        self.text = helpers.chapter_text(2, 960, 1500, "麦田")
        self.plan = helpers.plan_payload(2)

    def test_rule_extraction_never_invents_state_changes_by_default(self):
        d = ex.rule_extract(2, self.text, plan=self.plan, canon=self.canon)
        self.assertEqual(d["character_changes"], [],
                         "a plan prediction is not a fact; it must not write canon")
        codes = [u["code"] for u in d["uncertain"]]
        self.assertIn("PLAN_STATE_CHANGE_UNVERIFIED", codes)
        self.assertTrue(d["events"], "at least one event must be derived")

    def test_rule_extraction_records_present_entities(self):
        d = ex.rule_extract(2, self.text, plan=self.plan, canon=self.canon)
        names = {c["name"] for c in d["entities"]["characters"]}
        self.assertIn("李禾", names)
        self.assertTrue(d["entities"]["locations"])

    def test_plan_state_source_opts_in_with_a_warning(self):
        d = ex.rule_extract(2, self.text, plan=self.plan, canon=self.canon,
                            state_source="plan")
        self.assertTrue(d["character_changes"])
        self.assertIn("PLAN_DERIVED_STATE_CHANGE", [w["code"] for w in d["warnings"]])

    def test_missing_timeline_is_a_warning(self):
        plan = dict(self.plan)
        plan.pop("timeline", None)
        d = ex.rule_extract(2, self.text, plan=plan, canon=self.canon)
        self.assertIn("TIMELINE_NOT_ADVANCED", [w["code"] for w in d["warnings"]])

    def test_human_summary_is_derived_from_the_delta(self):
        d = helpers.delta_payload(2)
        d["foreshadow_changes"] = [{"code": "F001", "before": "ACTIVE", "after": "RESOLVED"}]
        s = ex.human_summary(d)
        self.assertIn("F001", s)
        self.assertIn("事件", s)

    def test_plot_repetition_detector(self):
        delta = helpers.delta_payload(2)
        prior = [helpers.scene_payload(1, helpers.LOCATIONS[0], ("李禾", "沈默"))]
        prior[0]["chapter"] = 1
        delta["scenes"] = [helpers.scene_payload(1, helpers.LOCATIONS[0], ("李禾", "沈默"))]
        hits = ex.plot_repetition(delta, prior)
        self.assertTrue(hits)
        self.assertEqual(hits[0]["code"], "POSSIBLE_PLOT_REPETITION")


class LlmBoundaryTest(unittest.TestCase):
    def test_json_block_survives_fences_and_chatter(self):
        self.assertEqual(llm_mod.extract_json_block('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(llm_mod.extract_json_block('好的，结果如下：{"a": 2} 谢谢'),
                         {"a": 2})

    def test_truncated_json_raises(self):
        with self.assertRaises(Exception):
            llm_mod.extract_json_block('{"a": 1')

    def test_missing_file_provider_raises(self):
        with self.assertRaises(Exception):
            llm_mod.run_json("file:/definitely/not/here.json", "prompt")

    def test_rule_provider_has_no_model(self):
        with self.assertRaises(Exception):
            llm_mod.run_json("rule", "prompt")


if __name__ == "__main__":
    unittest.main()
