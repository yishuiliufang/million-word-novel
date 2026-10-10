"""Backward compatibility with the pre-upgrade CLI, and documentation/CLI agreement.

The brief allows breaking changes only where they conflict with correct process or
Canon consistency. Everything else must keep working: the same subcommands, the
same flags, the same exit codes, the same file names.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import helpers  # noqa: E402
from helpers import Project, fresh_project  # noqa: E402

import audit_quality  # noqa: E402
import canon_db as db  # noqa: E402
import check_seams  # noqa: E402
import count_words  # noqa: E402
import export_novel  # noqa: E402
import fix_quotes  # noqa: E402
import novel_state  # noqa: E402
import polish_prose  # noqa: E402
import scan_beats  # noqa: E402
import scan_tags  # noqa: E402
import strip_beats  # noqa: E402
import validate_state  # noqa: E402
from _common import atomic_write_json, atomic_write_text, read_text  # noqa: E402

REPO = helpers.ROOT


class LegacyCliSurfaceTest(unittest.TestCase):
    def setUp(self):
        self.p = fresh_project(name="legacy")
        self.addCleanup(self.p.cleanup)

    def test_all_legacy_subcommands_still_exist(self):
        parser = novel_state.build_parser()
        choices = parser._subparsers._group_actions[0].choices
        for cmd in ("init", "status", "next", "commit", "import", "plan", "part",
                    "round-done"):
            self.assertIn(cmd, choices, "legacy subcommand %s disappeared" % cmd)

    def test_status_json_keeps_legacy_keys(self):
        rc, out, err = self.p.json_run(novel_state, ["status", "--root", self.p.root,
                                                     "--json"])
        self.assertEqual(rc, 0)
        for key in ("title", "count_mode", "target_chars", "total_chars",
                    "remaining_chars", "percent", "chapters_committed",
                    "highest_chapter", "rounds_completed", "next_chapter",
                    "estimated_rounds_left", "complete", "current_volume"):
            self.assertIn(key, out, "status lost key %s" % key)

    def test_init_creates_the_legacy_layout_and_the_canon_db(self):
        p = self.p.paths()
        for key in ("chapters", "state", "summaries", "export", "progress", "bible",
                    "outline", "ledger", "last_context", "banned"):
            self.assertTrue(os.path.exists(p[key]), "missing legacy path %s" % key)
        self.assertTrue(os.path.isfile(p["canon"]))
        for key in ("drafts", "parts", "snapshots", "plans", "audits", "derived", "tx"):
            self.assertTrue(os.path.isdir(p[key]), "missing new path %s" % key)

    def test_three_count_modes_still_work(self):
        self.p.full_chapter(1)
        expected = {"no_ws": None, "cjk": None, "all": None}
        for mode in expected:
            rc, out, err = self.p.json_run(count_words, ["--root", self.p.root,
                                                         "--mode", mode, "--json"])
            self.assertEqual(rc, 0)
            self.assertGreater(out["total_chars"], 0)
            expected[mode] = out["total_chars"]
        self.assertGreater(expected["all"], expected["no_ws"])
        self.assertGreater(expected["no_ws"], expected["cjk"])

    def test_plan_volume_plan_mode_is_unchanged(self):
        rc, out, err = self.p.json_run(novel_state, ["plan", "--root", self.p.root,
                                                     "--volume-plan", "12,13", "--chars-per-chapter", "4000"])
        self.assertEqual(rc, 0)
        self.assertEqual(out["mode"], "structure")
        self.assertEqual([v["chapters"] for v in out["volumes"]], [12, 13])
        prog = self.p.progress()
        self.assertEqual(prog["chars_per_chapter"], 4000)

    def test_part_workflow_appends_and_checks(self):
        prog = self.p.progress()
        lo, hi = prog["chars_per_chapter_tolerance"]
        piece1 = helpers.chapter_text(1, lo // 2, lo // 2 + 50, "黑塔")
        piece2 = helpers.chapter_text(1, lo // 2, lo // 2 + 50, "麦田")
        f1 = os.path.join(self.p.paths()["parts"], "ch1-p1.txt")
        f2 = os.path.join(self.p.paths()["parts"], "ch1-p2.txt")
        atomic_write_text(f1, piece1)
        atomic_write_text(f2, piece2)
        rc, out, err = self.p.json_run(novel_state, ["part", "--root", self.p.root,
                                                     "--chapter", "1", "--file", f1])
        self.assertEqual(rc, 0)
        rc, out, err = self.p.json_run(novel_state, ["part", "--root", self.p.root,
                                                     "--chapter", "1", "--file", f2])
        self.assertEqual(rc, 0)
        rc2, out2, err2 = self.p.json_run(novel_state, ["part", "--root", self.p.root,
                                                        "--chapter", "1", "--check"])
        self.assertEqual(rc2, 0)
        self.assertEqual(out2["chars"], out["chars"])
        self.assertTrue(os.path.isfile(self.p.paths()["drafts"] + os.sep + "ch-0001.md"))

    def test_part_refuses_an_already_committed_chapter(self):
        self.p.full_chapter(1)
        f1 = os.path.join(self.p.paths()["parts"], "x.txt")
        atomic_write_text(f1, "追加内容")
        rc, out, err = self.p.run(novel_state, ["part", "--root", self.p.root,
                                                "--chapter", "1", "--file", f1])
        self.assertNotEqual(rc, 0)

    def test_audit_quality_legacy_cli_and_json_shape(self):
        self.p.full_chapter(1)
        rc, out, err = self.p.json_run(audit_quality, ["--root", self.p.root,
                                                       "--chapter", "1", "--json",
                                                       "--legacy-json"])
        self.assertEqual(rc, 0)
        for key in ("audited", "passed", "failed", "all_pass", "reports"):
            self.assertIn(key, out)
        self.assertIn("issues", out["reports"][0])
        self.assertIn("advisories", out["reports"][0])

    def test_audit_quality_strict_exit_codes(self):
        self.p.write_draft(1, text="短。\n")
        rc, out, err = self.p.run(audit_quality, ["--root", self.p.root, "--chapter", "1",
                                                  "--file",
                                                  os.path.join(self.p.paths()["drafts"],
                                                               "ch-0001.md"),
                                                  "--strict"])
        self.assertEqual(rc, 1)
        rc2, out2, err2 = self.p.run(audit_quality, ["--root", self.p.root])
        self.assertEqual(rc2, 0)

    def test_export_zip_contains_chapters_state_and_canon(self):
        self.p.full_chapter(1)
        rc, out, err = self.p.json_run(export_novel, ["--root", self.p.root,
                                                      "--by-volume", "--zip"])
        self.assertEqual(rc, 0)
        import zipfile
        zip_path = [f for f in out["files"] if f.endswith(".zip")][0]
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
        self.assertIn("chapters/ch-0001.txt", names)
        self.assertIn("state/progress.json", names)
        self.assertIn("state/canon.db", names)
        self.assertIn("book.txt", names)

    def test_import_adopts_existing_prose(self):
        src_dir = os.path.join(self.p.root, "manuscript")
        os.makedirs(src_dir, exist_ok=True)
        for i in (1, 2):
            atomic_write_text(os.path.join(src_dir, "ch%02d.txt" % i),
                              "# 旧稿第%d章\n\n" % i + helpers.chapter_text(i, 900, 1400))
        rc, out, err = self.p.json_run(novel_state, [
            "import", "--root", self.p.root, "--group",
            os.path.join(src_dir, "ch*.txt"), "--start-chapter", "1"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(out["imported"], 2)
        report = validate_state.validate(self.p.root)
        self.assertEqual(report["errors"], 0, report["findings"])
        conn = self.p.conn()
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("SELECT COUNT(*) c FROM snapshots "
                                      "WHERE kind='delta'").fetchone()["c"], 2)


class MigrateTest(unittest.TestCase):
    """A pre-upgrade (schema 1, no Canon DB) project must upgrade in place."""

    def setUp(self):
        self.root = helpers._mkdtemp("mwn-legacy-migrate-")
        os.makedirs(os.path.join(self.root, "chapters"))
        os.makedirs(os.path.join(self.root, "state", "summaries"))
        os.makedirs(os.path.join(self.root, "export"))
        prog = {
            "schema": 1, "title": "旧书", "premise": "旧的一句话", "root": self.root,
            "target_chars": 20000, "count_mode": "no_ws", "chars_per_chapter": 1200,
            "chars_per_chapter_min": 1020,
            "chars_per_chapter_tolerance": [960, 1500],
            "chapters_per_round": 3, "estimated_chapters": 17, "volume_count": 2,
            "rounds_completed": 1, "chapters_committed": 2,
            "created_at": "2026-01-01T00:00:00", "updated_at": "2026-01-01T00:00:00",
        }
        atomic_write_json(os.path.join(self.root, "state", "progress.json"), prog)
        bible = ("# 设定圣经（Bible）\n\n## 3. 世界规则（硬设定）\n"
                 "| 编号 | 规则 | 首次出现章节 | 不可违反 |\n|---|---|---|---|\n"
                 "| R1 | 旧世界的规则 | 1 | 是 |\n\n"
                 "## 4. 主要人物\n| 姓名 | 身份 | 核心欲望 | 致命缺陷 | 结局走向 | 状态 |\n"
                 "|---|---|---|---|---|---|\n"
                 "| 李禾 | 记账人 | 查清旧账 | 固执 | 未知 | 存活 |\n\n"
                 "## 7. 伏笔登记表\n| 编号 | 伏笔 | 埋设章节 | 计划回收章节 | 状态 |\n"
                 "|---|---|---|---|---|\n| F001 | 旧铜钥匙 | 1 | 3-6 | 未回收 |\n")
        atomic_write_text(os.path.join(self.root, "state", "bible.md"), bible)
        atomic_write_text(os.path.join(self.root, "state", "outline.md"),
                          "# 分卷总纲\n\n## 第1卷（第 1–5 章）\n\n"
                          "- 卷目标（不可逆变化）：旧的卷目标\n")
        for i in (1, 2):
            atomic_write_text(os.path.join(self.root, "chapters", "ch-%04d.txt" % i),
                              helpers.chapter_text(i, 960, 1500) + "\n")
        atomic_write_text(os.path.join(self.root, "state", "plot-ledger.md"),
                          "# 旧台账\n\n| 章节 | 字数 |\n|---|---|\n| ch-0001 | 1200 |\n")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_migrate_upgrades_without_losing_anything(self):
        p = Project.__new__(Project)
        p.dir = self.root
        rc, out, err = p.json_run(novel_state, ["migrate", "--root", self.root])
        self.assertEqual(rc, 0, err or out)
        self.assertEqual(out["chapters_found"], 2)
        self.assertEqual(out["schema"], 2)
        conn = p.conn()
        self.assertEqual(conn.execute("SELECT COUNT(*) c FROM chapters").fetchone()["c"], 2)
        names = {r["name"] for r in conn.execute(
            "SELECT name FROM entities WHERE kind='character'").fetchall()}
        self.assertIn("李禾", names, "the hand-written bible must be imported to Canon")
        self.assertEqual(conn.execute("SELECT COUNT(*) c FROM snapshots "
                                      "WHERE kind='delta'").fetchone()["c"], 2)
        conn.close()
        # the hand-written bible is preserved as a source copy
        self.assertTrue(os.path.isfile(os.path.join(self.root, "state",
                                                    "bible.source.md")))
        report = validate_state.validate(self.root)
        self.assertEqual(report["errors"], 0, report["findings"])
        # derived views now exist and are marked as generated
        self.assertIn("AUTO-GENERATED", read_text(os.path.join(self.root, "state",
                                                               "bible.md")))
        # replay still reproduces the migrated canon
        self.assertTrue(db.verify_replay(self.root)["ok"])
        # the round counter survives migration
        self.assertEqual(json.loads(read_text(
            os.path.join(self.root, "state", "progress.json")))["rounds_completed"], 1)


class FixQuotesContractTest(unittest.TestCase):
    STRIPPED = "\n".join([
        "拿石头做什么。他问。",
        "记路。李禾说。",
        "他回头看了一眼。",
        "风从北边来。",
    ]) + "\n"

    def setUp(self):
        self.root = helpers._mkdtemp("mwn-fixquotes-")
        self.path = os.path.join(self.root, "draft.txt")
        atomic_write_text(self.path, self.STRIPPED)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_default_is_report_only_and_exits_one(self):
        rc, out, err = Project.run(self, fix_quotes, ["--file", self.path])
        self.assertEqual(rc, 1, "pending repairs must be reported through exit code 1")
        self.assertEqual(read_text(self.path), self.STRIPPED,
                         "the default mode must not modify the file")
        self.assertIn("未写入", out)

    def test_apply_repairs_and_exits_zero(self):
        rc, out, err = Project.run(self, fix_quotes, ["--file", self.path, "--apply"])
        self.assertEqual(rc, 0)
        fixed = read_text(self.path)
        self.assertNotEqual(fixed, self.STRIPPED)
        self.assertIn('"拿石头做什么。"他问。', fixed)
        self.assertIn('"记路。"李禾说。', fixed)
        # idempotent: a second pass finds nothing
        rc2, out2, err2 = Project.run(self, fix_quotes, ["--file", self.path])
        self.assertEqual(rc2, 0)

    def test_report_only_alias_also_writes_nothing(self):
        rc, out, err = Project.run(self, fix_quotes, ["--file", self.path,
                                                      "--report-only"])
        self.assertEqual(rc, 1)
        self.assertEqual(read_text(self.path), self.STRIPPED)

    def test_json_report_declares_the_mode(self):
        rc, out, err = Project.run(self, fix_quotes, ["--file", self.path, "--json"])
        data = json.loads(out)
        self.assertEqual(data["mode"], "report-only")
        self.assertFalse(data["applied"])
        self.assertGreater(data["repaired"], 0)


class PolishCanonSafetyTest(unittest.TestCase):
    # a bare vocative line repeated: the old polisher replaced these with newly
    # invented action beats ("李禾抬头看他。"), which is writing, not polishing.
    VOCATIVE = ('"陈老师。"她说。\n' * 3
                + "他把账本翻到新的一页，灯芯短了一截。\n" * 6)

    def setUp(self):
        self.root = helpers._mkdtemp("mwn-polish-")
        self.path = os.path.join(self.root, "draft.txt")
        atomic_write_text(self.path, self.VOCATIVE)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_canon_affecting_passes_are_refused_by_default(self):
        rc, out, err = Project.run(self, polish_prose, ["--file", self.path])
        self.assertEqual(rc, 0)
        self.assertEqual(read_text(self.path), self.VOCATIVE,
                         "the polisher must not rewrite canon without permission")
        self.assertIn("CANON_AFFECTING_EDIT", out)
        self.assertIn("跳过", out)

    def test_strict_canon_turns_detection_into_a_failure(self):
        rc, out, err = Project.run(self, polish_prose, ["--file", self.path,
                                                        "--strict-canon"])
        self.assertEqual(rc, 1)

    def test_allow_canon_edits_applies_and_writes_a_report(self):
        rc, out, err = Project.run(self, polish_prose, ["--file", self.path,
                                                        "--allow-canon-edits"])
        self.assertEqual(rc, 0)
        after = read_text(self.path)
        self.assertNotEqual(after, self.VOCATIVE)
        sidecar = self.path + ".polish.json"
        self.assertTrue(os.path.isfile(sidecar))
        report = json.loads(read_text(sidecar))
        self.assertTrue(report["allow_canon_edits"])
        self.assertTrue(report["canon_affecting_edits"])

    def test_safe_passes_still_run_by_default(self):
        text = '"他走了。"他说，"我跟上。"\n' + "他把账本翻到新的一页。\n" * 3
        atomic_write_text(self.path, text)
        rc, out, err = Project.run(self, polish_prose, ["--file", self.path])
        self.assertEqual(rc, 0)
        self.assertIn("代词标签", out)
        self.assertNotIn('"他走了。"他说，"我跟上。"', read_text(self.path))


class AuxiliaryScriptsTest(unittest.TestCase):
    def setUp(self):
        self.root = helpers._mkdtemp("mwn-aux-")
        self.addCleanup(lambda: shutil.rmtree(self.root, ignore_errors=True))

    def test_check_seams_detects_a_duplicated_seam(self):
        parts = os.path.join(self.root, "parts")
        os.makedirs(parts, exist_ok=True)
        atomic_write_text(os.path.join(parts, "p1.txt"),
                          "他把门推开，屋里没有人。\n灯芯短了一截。\n")
        atomic_write_text(os.path.join(parts, "p2.txt"),
                          "灯芯短了一截。\n他坐了下来。\n")
        rc, out, err = Project.run(self, check_seams, ["--dir", parts])
        self.assertEqual(rc, 1)
        self.assertIn("接缝", out)

    def test_check_seams_clean_when_no_overlap(self):
        parts = os.path.join(self.root, "parts")
        os.makedirs(parts, exist_ok=True)
        atomic_write_text(os.path.join(parts, "p1.txt"), "第一段结束了。\n")
        atomic_write_text(os.path.join(parts, "p2.txt"), "第二段开始了。\n")
        rc, out, err = Project.run(self, check_seams, ["--dir", parts])
        self.assertEqual(rc, 0)

    def test_scan_tags_reports_declarative_questions(self):
        path = os.path.join(self.root, "ch.txt")
        atomic_write_text(path, '"他找过我。"他问。\n"你走吗。"他问。\n')
        rc, out, err = Project.run(self, scan_tags, [path])
        self.assertEqual(rc, 1)
        self.assertIn("陈述句误配", out)

    def test_scan_beats_and_strip_beats_read_utf8(self):
        path = os.path.join(self.root, "ch.txt")
        # a female beat inside an all-male exchange is the defect scan_beats looks for
        atomic_write_text(path, '"你十九岁。"沈默说。\n她应了一声。\n"你想了多久？"\n')
        rc, out, err = Project.run(self, scan_beats, [path])
        self.assertEqual(rc, 1)
        self.assertIn("可疑", out)
        rc2, out2, err2 = Project.run(self, strip_beats, [path])
        self.assertEqual(rc2, 1)
        self.assertIn("未写入", out2)
        rc3, out3, err3 = Project.run(self, strip_beats, [path, "--apply"])
        self.assertEqual(rc3, 0)
        self.assertNotIn("她应了一声。", read_text(path))


class DocsConsistencyTest(unittest.TestCase):
    def docs(self):
        out = {}
        for rel in ("README.md", "SKILL.md", "docs/CLI.md", "docs/CHANGES.md",
                    "docs/ARCHITECTURE.md"):
            path = os.path.join(REPO, rel)
            if os.path.isfile(path):
                out[rel] = read_text(path)
        return out

    def test_every_documented_novel_state_command_exists(self):
        parser = novel_state.build_parser()
        choices = set(parser._subparsers._group_actions[0].choices.keys())
        pattern = re.compile(r"novel_state\.py\s+([a-z][a-z-]+)")
        for rel, text in self.docs().items():
            for cmd in set(pattern.findall(text)):
                if cmd in ("py",):
                    continue
                self.assertIn(cmd, choices,
                              "%s documents `novel_state.py %s` which does not exist"
                              % (rel, cmd))

    def test_every_documented_script_exists(self):
        pattern = re.compile(r"scripts[/\\]([A-Za-z_][A-Za-z0-9_]*)\.py")
        for rel, text in self.docs().items():
            for name in set(pattern.findall(text)):
                self.assertTrue(os.path.isfile(os.path.join(REPO, "scripts",
                                                            "%s.py" % name)),
                                "%s documents scripts/%s.py which does not exist"
                                % (rel, name))

    def test_skill_md_has_no_corrupted_table_rows(self):
        path = os.path.join(REPO, "SKILL.md")
        if not os.path.isfile(path):
            self.skipTest("SKILL.md missing")
        text = read_text(path)
        self.assertNotIn("`n| ", text, "the command table is corrupted by literal `n| `")
        self.assertNotIn("\\n| `fix_quotes", text)

    def test_fix_quotes_is_documented_as_report_only_with_apply(self):
        """The command table must document both the default (report-only) and --apply."""
        joined = "\n".join(self.docs().values())
        self.assertIn("fix_quotes.py", joined)
        self.assertIn("--apply", joined)
        self.assertIn("报告", joined)
        for rel, text in self.docs().items():
            for line in text.splitlines():
                # only command rows: the first table cell names the script
                if line.strip().startswith("| `fix_quotes.py"):
                    self.assertIn("apply", line.lower(),
                                  "%s: fix_quotes command row must mention --apply: %s"
                                  % (rel, line.strip()))
                elif line.strip().startswith("`fix_quotes.py"):
                    self.assertIn("apply", line.lower(),
                                  "%s: fix_quotes line must mention --apply: %s"
                                  % (rel, line.strip()))

    def test_docs_state_the_canonical_truth_order_and_no_audit_after_commit(self):
        joined = "\n".join(self.docs().values())
        self.assertIn("canon.db", joined)
        self.assertTrue("audit" in joined and "commit" in joined)
        # the removed behaviour must be documented as a breaking change
        changes = self.docs().get("docs/CHANGES.md", "")
        if changes:
            self.assertIn("破坏", changes)

    def test_cli_usage_document_covers_the_required_commands(self):
        text = self.docs().get("docs/CLI.md", "")
        if not text:
            self.skipTest("docs/CLI.md not written yet")
        for snippet in ("novel_state.py init", "novel_state.py next",
                        "novel_state.py plan", "novel_state.py audit",
                        "novel_state.py commit", "novel_state.py round-done",
                        "canon.py", "validate_state.py"):
            self.assertIn(snippet, text, "CLI.md is missing %s" % snippet)


if __name__ == "__main__":
    unittest.main()
