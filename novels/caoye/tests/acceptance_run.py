#!/usr/bin/env python3
"""Mechanical acceptance run for the Long-Context Narrative OS.

Runs the REAL command-line tools (subprocess, not in-process calls) against a
throw-away project and records, for every required acceptance item: the exact
command, the exit code, and whether the expected artifacts exist. Output is a
human summary plus a JSON report used by docs/ACCEPTANCE.md.

    python tests/acceptance_run.py [--chapters 6] [--keep]
"""
from __future__ import annotations

import argparse
import compileall
import json
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import helpers  # noqa: E402  (fixtures: bible text, chapter prose)

PY = sys.executable
TMP = os.path.join(ROOT, ".tmp-tests")
RECORDS = []
CHECKS = []


def record(kind, name, ok, detail=""):
    RECORDS.append({"kind": kind, "name": name, "ok": bool(ok), "detail": detail})
    mark = "PASS" if ok else "FAIL"
    print("  [%s] %s%s" % (mark, name, (" — %s" % detail) if detail else ""))
    return ok


def run(args, expect=0, label=None, cwd=ROOT):
    """Run one CLI command; record command, exit code and stdout tail."""
    cmd = [PY] + args
    proc = subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    out = proc.stdout.decode("utf-8", "replace")
    err = proc.stderr.decode("utf-8", "replace")
    label = label or " ".join(os.path.basename(a) if a.endswith(".py") else a for a in args)
    ok = (expect is None) or (proc.returncode == expect)
    RECORDS.append({
        "kind": "command", "name": label,
        "command": "%s %s" % (os.path.basename(PY), " ".join(args)),
        "exit_code": proc.returncode, "expected_exit_code": expect,
        "ok": ok, "stdout_tail": out[-900:], "stderr_tail": err[-400:],
    })
    print("  [%s] exit=%s (期望 %s)  %s"
          % ("PASS" if ok else "FAIL", proc.returncode, expect, label))
    if not ok:
        print("        stderr: %s" % err.strip()[:300])
    return proc.returncode, out, err


def jload(out):
    try:
        return json.loads(out)
    except ValueError:
        for line in reversed([ln for ln in out.splitlines() if ln.strip()]):
            try:
                return json.loads(line)
            except ValueError:
                continue
        return {}


def jdump(path, obj):
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def exists(*paths):
    return all(os.path.exists(p) for p in paths)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chapters", type=int, default=6)
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--out", default=os.path.join(ROOT, "docs", "acceptance-report.json"))
    args = ap.parse_args(argv)

    started = time.time()
    n = max(5, args.chapters)
    root = helpers._mkdtemp("mwn-acceptance-")
    print("=" * 78)
    print("验收运行 / ACCEPTANCE RUN")
    print("  项目目录 : %s" % root)
    print("  章节数   : %d（要求 5~10）" % n)
    print("  Python   : %s" % sys.version.split()[0])
    print("=" * 78)

    # ---------------------------------------------------------------- 1. syntax
    print("\n[1] Python 语法检查")
    ok = compileall.compile_dir(os.path.join(ROOT, "scripts"), quiet=2, force=True)
    record("syntax", "compileall scripts/", ok)
    py_files = sorted(f for f in os.listdir(os.path.join(ROOT, "scripts"))
                      if f.endswith(".py"))
    record("syntax", "脚本数量", len(py_files) >= 20, "%d 个 .py" % len(py_files))

    # ---------------------------------------------------------------- 2. init
    print("\n[2] init：项目骨架 + Canon DB")
    run(["scripts/novel_state.py", "init", "--root", root, "--title", "验收小说",
         "--premise", "一句话故事", "--target", str(1200 * n), "--chars-per-chapter",
         "1200", "--chapters-per-round", "3", "--volumes", "2"], 0, "init")
    p = helpers.paths(root)
    record("artifact", "目录与文件齐备",
           exists(p["canon"], p["chapters"], p["drafts"], p["parts"], p["snapshots"],
                  p["plans"], p["audits"], p["derived"], p["tx"], p["progress"],
                  p["bible"], p["outline"], p["ledger"], p["banned"],
                  os.path.join(root, "tests", "helpers.py")),
           "canon.db + chapters/drafts/parts/state{s,snapshots,plans,audits,derived,tx}")

    # ---------------------------------------------------------------- 3. bible -> canon
    print("\n[3] 手工圣经 → Canon (import-md)")
    bible = open(p["bible"], encoding="utf-8").read()
    text = ["# 设定圣经（Bible）", "", "## 3. 世界规则（硬设定）",
            "| 编号 | 规则 | 首次出现章节 | 不可违反 |", "|---|---|---|---|",
            "| R1 | 账本上的数字会改变现实 | 1 | 是 |", "", "## 4. 主要人物",
            "| 姓名 | 身份 | 核心欲望 | 致命缺陷 | 结局走向 | 状态 |",
            "|---|---|---|---|---|---|"]
    for i, name in enumerate(helpers.CHARACTERS):
        text.append("| %s | 记账人%d | 查清账本的来历 | 不肯认错 | 未知 | 存活 |" % (name, i))
    text += ["", "## 5. 地理与组织", "| 名称 | 类型 | 说明 |", "|---|---|---|"]
    for loc in helpers.LOCATIONS:
        text.append("| %s | 地点 | 验收用地点 |" % loc)
    text += ["", "## 7. 伏笔登记表",
             "| 编号 | 伏笔 | 埋设章节 | 计划回收章节 | 状态 |", "|---|---|---|---|---|",
             "| F001 | 铜钥匙的来历 | 1 | 3-5 | 未回收 |", "", "## 8. 剧情线程",
             "| 编号 | 名称 | 类型 | 目标 | 状态 |", "|---|---|---|---|---|",
             "| T1 | 找到种子库 | main | 找到并打开种子库 | planned |"]
    open(p["bible"], "w", encoding="utf-8", newline="\n").write("\n".join(text) + "\n")
    outline = ["# 分卷总纲", "", "## 第1卷（第 1–5 章）", "",
               "- 卷目标（不可逆变化）：查清账本的来历",
               "- 主要冲突：账本与现实的对账",
               "- 关键转折：第二本账出现",
               "- 卷末钩子：有人替他签过名",
               "- 卷末不可逆变化：李禾再也回不到不知道账本存在的生活", "",
               "## 第2卷（第 6–10 章）", "",
               "- 卷目标（不可逆变化）：找到种子库",
               "- 卷末不可逆变化：沈默的名字从账本上消失", ""]
    open(p["outline"], "w", encoding="utf-8", newline="\n").write("\n".join(outline))
    run(["scripts/canon.py", "--root", root, "import-md"], 0, "canon import-md")
    rc, out, _ = run(["scripts/canon.py", "--root", root, "show", "--json"], 0,
                     "canon show --json")
    show = jload(out)
    record("canon", "人物/规则/伏笔/线程已入库",
           bool(show.get("entities")) and show.get("world_rules")
           and show.get("foreshadowing", {}).get("total") == 1,
           "entities=%s rules=%d foreshadowing=%s"
           % (show.get("entities"), len(show.get("world_rules") or []),
              show.get("foreshadowing")))
    record("canon", "bible.md 已转为派生视图",
           "AUTO-GENERATED" in open(p["bible"], encoding="utf-8").read())
    record("canon", "手写原件已备份", os.path.isfile(os.path.join(p["state"],
                                                              "bible.source.md")))

    # ---------------------------------------------------------------- 4. next
    print("\n[4] next：四层记忆简报 + 轮次计划")
    rc, out, _ = run(["scripts/novel_state.py", "next", "--root", root], 0, "next")
    brief = jload(out)
    record("brief", "action=WRITE 且给出本轮章节", brief.get("action") == "WRITE"
           and bool(brief.get("chapters_to_write")), str(brief.get("chapters_to_write")))
    record("brief", "四层记忆齐备",
           all(k in (brief.get("context") or {}) for k in
               ("L0_current", "L1_local", "L2_structure", "L3_canon")))
    record("brief", "legacy 键保留",
           all(k in brief for k in ("chapters_to_write", "chars_tolerance", "bible_digest",
                                    "ledger_tail", "previous_chapter_tail", "hard_rules")))
    record("brief", "outline 参与 L2",
           bool((brief.get("context") or {}).get("L2_structure", {}).get("volume_goal")),
           "volume_goal=%r" % ((brief.get("context") or {}).get("L2_structure", {})
                               .get("volume_goal")))

    # ---------------------------------------------------------------- 5. order proof
    print("\n[5] audit → commit 顺序证明")
    plan_path = os.path.join(p["plans"], "ch-0001.plan.json")
    delta_path = os.path.join(p["plans"], "ch-0001.delta.json")
    jdump(plan_path, helpers.plan_payload(1))
    run(["scripts/novel_state.py", "plan", "--root", root, "--chapter", "1",
         "--file", plan_path, "--check"], 0, "plan --check (chapter 1)")
    jdump(delta_path, helpers.delta_payload(1))
    lo, hi = 960, 1500
    draft1 = os.path.join(p["drafts"], "ch-0001.md")
    open(draft1, "w", encoding="utf-8", newline="\n").write(
        helpers.chapter_text(1, lo, hi, helpers.LOCATIONS[0]))
    rc, out, err = run(["scripts/novel_state.py", "commit", "--root", root,
                        "--chapter", "1", "--file", draft1], 2,
                       "commit 未审计 → 必须被拒 (exit 2)")
    record("order", "未审计即提交被拒绝", rc == 2 and "audit" in err)
    record("order", "被拒的提交不留下章节文件",
           not os.path.isfile(os.path.join(p["chapters"], "ch-0001.txt")))
    run(["scripts/novel_state.py", "audit", "--root", root, "--chapter", "1",
         "--file", draft1, "--json", "--strict"], 0, "audit --strict")
    draft1_text = open(draft1, encoding="utf-8").read()
    open(draft1, "a", encoding="utf-8", newline="\n").write("他又补了一句。\n")
    rc, out, err = run(["scripts/novel_state.py", "commit", "--root", root,
                        "--chapter", "1", "--file", draft1], 2,
                       "审计后改稿 → 必须被拒 (exit 2)")
    record("order", "审计后改稿使审计失效", rc == 2 and "审计" in err)
    open(draft1, "w", encoding="utf-8", newline="\n").write(draft1_text)
    run(["scripts/novel_state.py", "audit", "--root", root, "--chapter", "1",
         "--file", draft1, "--json", "--strict"], 0, "audit 重新执行")
    run(["scripts/novel_state.py", "commit", "--root", root, "--chapter", "1"],
        0, "commit (plan+audit 齐备)")
    record("artifact", "章节文件 / 快照 / canon 行",
           exists(os.path.join(p["chapters"], "ch-0001.txt"),
                  os.path.join(p["snapshots"], "ch-0001.json")),
           "chapters/ch-0001.txt + snapshots/ch-0001.json")

    # ---------------------------------------------------------------- 6. auto-next 5~n chapters
    print("\n[6] auto-next：真实 CLI 连续跑 %d 章" % n)
    writer = '%s "%s" write' % (PY, os.path.join(HERE, "fake_writer.py"))
    extractor = '%s "%s" extract' % (PY, os.path.join(HERE, "fake_writer.py"))
    rc, out, err = run(["scripts/novel_state.py", "auto-next", "--root", root,
                        "--writer", writer, "--extractor", extractor,
                        "--max-chapters", str(n)], 0,
                       "auto-next --max-chapters %d" % n)
    chapters = sorted(int(f[3:7]) for f in os.listdir(p["chapters"])
                      if f.startswith("ch-") and f.endswith(".txt"))
    record("e2e", "已提交章节数", len(chapters) >= 5, "%d 章：%s" % (len(chapters), chapters))
    record("e2e", "章节号连续", chapters == list(range(1, len(chapters) + 1)))
    snaps = [f for f in os.listdir(p["snapshots"]) if f.startswith("ch-")]
    record("e2e", "每章都有 canon snapshot", len(snaps) == len(chapters),
           "%d 个快照" % len(snaps))

    # ---------------------------------------------------------------- 7. validate
    print("\n[7] 状态校验 validate_state.py")
    rc, out, _ = run(["scripts/validate_state.py", "--root", root, "--json"], 0,
                     "validate_state --json")
    report = jload(out)
    record("validate", "0 error", report.get("errors") == 0,
           "errors=%s warnings=%s" % (report.get("errors"), report.get("warnings")))

    # ---------------------------------------------------------------- 8. replay proof
    print("\n[8] 快照回放可重建 canon.db（verify-snapshots）")
    rc, out, _ = run(["scripts/canon.py", "--root", root, "verify-snapshots", "--json"],
                     0, "canon verify-snapshots")
    verify = jload(out)
    record("snapshot", "PASS：快照日志完整重建 Canon", verify.get("ok") is True,
           "回放 %s 章 / 比较 %s 张逻辑表"
           % (verify.get("chapters_replayed"), verify.get("tables_compared")))

    # ---------------------------------------------------------------- 9. schema inventory
    print("\n[9] Canon DB 表结构清单（表名/字段/索引）")
    rc, out, _ = run(["scripts/canon.py", "--root", root, "schema", "--json"], 0,
                     "canon schema --json")
    schema = jload(out)
    tables = [t["table"] for t in schema] if isinstance(schema, list) else []
    n_cols = sum(len(t["columns"]) for t in schema) if isinstance(schema, list) else 0
    n_idx = sum(len(t["indexes"]) for t in schema) if isinstance(schema, list) else 0
    record("schema", "表数量 ≥ 20", len(tables) >= 20,
           "%d 张表 / %d 字段 / %d 索引" % (len(tables), n_cols, n_idx))
    for required in ("chapters", "entities", "character_states", "events",
                     "foreshadowings", "plot_threads", "world_rules", "knowledge",
                     "items", "snapshots", "plans", "audits", "gates", "transactions",
                     "round_runs", "canon_changes", "warnings", "scene_index", "timeline"):
        record("schema", "表 %s 存在" % required, required in tables)

    # ---------------------------------------------------------------- 10. rollback
    print("\n[10] rollback：回滚到第 %d 章" % (len(chapters) - 2))
    keep = len(chapters) - 2
    rc, out, _ = run(["scripts/novel_state.py", "rollback", "--root", root,
                      "--to-chapter", str(keep), "--reason", "验收：回滚测试"], 0,
                     "rollback --to-chapter %d" % keep)
    rb = jload(out)
    record("rollback", "被撤销章节已隔离而非删除",
           all(os.path.isfile(q) for q in (rb.get("chapter_files_quarantined") or []))
           and len(rb.get("chapter_files_quarantined") or []) == len(chapters) - keep,
           "隔离 %d 个章节文件" % len(rb.get("chapter_files_quarantined") or []))
    rc, out, _ = run(["scripts/validate_state.py", "--root", root, "--json"], 0,
                     "validate_state（回滚后）")
    record("rollback", "回滚后状态仍自洽", jload(out).get("errors") == 0)
    rc, out, _ = run(["scripts/canon.py", "--root", root, "verify-snapshots", "--json"],
                     0, "canon verify-snapshots（回滚后）")
    record("rollback", "回滚后快照仍可完整重建", jload(out).get("ok") is True)

    # ---------------------------------------------------------------- 11. round-done blocking
    print("\n[11] round-done 阻断非法状态")
    rounds_before = json.load(open(p["progress"], encoding="utf-8")).get("rounds_completed")
    rc, out, err = run(["scripts/novel_state.py", "round-done", "--root", root], 3,
                       "round-done（存在未完成轮次）")
    blocked = jload(out)
    record("round", "返回 ROUND_BLOCKED 且退出码 3", rc == 3
           and blocked.get("verdict") == "ROUND_BLOCKED",
           "blockers=%s" % (blocked.get("blockers") or [])[:2])
    rounds_after = json.load(open(p["progress"], encoding="utf-8")).get("rounds_completed")
    record("round", "阻断时不递增 rounds_completed", rounds_after == rounds_before,
           "rounds_completed %s → %s" % (rounds_before, rounds_after))

    # ---------------------------------------------------------------- 12. export
    print("\n[12] 导出成书")
    rc, out, _ = run(["scripts/export_novel.py", "--root", root, "--by-volume", "--zip"],
                     0, "export_novel --by-volume --zip")
    exported = jload(out)
    record("export", "导出文件存在",
           all(os.path.isfile(f) for f in exported.get("files") or []),
           "章节 %s / 文件 %d 个" % (exported.get("chapters"),
                                     len(exported.get("files") or [])))

    # ---------------------------------------------------------------- 13. legacy compat
    print("\n[13] 旧 CLI 兼容性抽查")
    rc, out, _ = run(["scripts/novel_state.py", "status", "--root", root, "--json"], 0,
                     "status --json")
    st = jload(out)
    legacy_keys = ("title", "count_mode", "target_chars", "total_chars", "remaining_chars",
                   "percent", "chapters_committed", "highest_chapter", "rounds_completed",
                   "next_chapter", "estimated_rounds_left", "complete", "current_volume")
    record("legacy", "status 旧键齐全", all(k in st for k in legacy_keys))
    rc, out, _ = run(["scripts/count_words.py", "--root", root, "--per-chapter", "--json"],
                     0, "count_words --per-chapter")
    record("legacy", "三种字数口径仍在",
           jload(out).get("total_chars", 0) > 0 and "chapters" in jload(out))
    rc, out, _ = run(["scripts/novel_state.py", "plan", "--root", root,
                      "--volume-plan", "10,10", "--chars-per-chapter", "1500"], 0,
                     "plan --volume-plan（旧模式）")
    record("legacy", "plan 旧模式仍可用", jload(out).get("mode") == "structure")
    manuscript = os.path.join(root, "manuscript")
    os.makedirs(manuscript, exist_ok=True)
    for i in (1, 2):
        open(os.path.join(manuscript, "old%02d.txt" % i), "w", encoding="utf-8",
             newline="\n").write(helpers.chapter_text(i, 900, 1400))
    current_max = max(int(f[3:7]) for f in os.listdir(p["chapters"])
                      if f.startswith("ch-") and f.endswith(".txt")) if chapters else 0
    run(["scripts/novel_state.py", "import", "--root", root, "--group",
         os.path.join(manuscript, "old*.txt"), "--start-chapter", str(current_max + 1)],
        0, "import --group（承接旧稿）")
    rc, out, _ = run(["scripts/validate_state.py", "--root", root, "--json"], 0,
                     "validate_state（导入后）")
    record("legacy", "导入章节不破坏状态校验",
           jload(out).get("errors") == 0, "errors=%s" % jload(out).get("errors"))

    # ---------------------------------------------------------------- 14. docs
    print("\n[14] 文档与 CLI 一致性")
    rc, out, _ = run(["tests/check_docs_cli.py"], 0, "docs/CLI 一致性检查")
    record("docs", "文档中的命令/脚本全部存在", rc == 0,
           (out.strip().splitlines() or [""])[-1])
    ddl_code = open(os.path.join(ROOT, "scripts", "schema.sql"), encoding="utf-8").read()
    ddl_doc = open(os.path.join(ROOT, "docs", "DDL.sql"), encoding="utf-8").read()
    record("docs", "docs/DDL.sql 与 scripts/schema.sql 一致", ddl_code == ddl_doc,
           "%d vs %d 字节" % (len(ddl_doc), len(ddl_code)))

    # ---------------------------------------------------------------- 15. tests
    print("\n[15] 测试套件结果")
    report_path = os.path.join(ROOT, "docs", "test-report.json")
    if os.path.isfile(report_path):
        tr = json.load(open(report_path, encoding="utf-8"))
        record("tests", "全部用例通过", tr.get("failed") == 0 and tr.get("total", 0) > 0,
               "%s/%s 通过，跳过 %s" % (tr.get("passed"), tr.get("total"),
                                       tr.get("skipped")))
    else:
        record("tests", "测试报告存在", False, "先运行 tests/run_tests.py")

    # ---------------------------------------------------------------- summary
    elapsed = time.time() - started
    failed = [r for r in RECORDS if not r["ok"]]
    summary = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "python": sys.version.split()[0],
        "project": root,
        "chapters": len(chapters),
        "elapsed_seconds": round(elapsed, 1),
        "checks_total": len(RECORDS),
        "checks_passed": len(RECORDS) - len(failed),
        "checks_failed": len(failed),
        "verdict": "PASS" if not failed else "FAIL",
        "schema": {"tables": tables, "columns": n_cols, "indexes": n_idx},
        "records": RECORDS,
    }
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=2)
        fh.write("\n")

    print("\n" + "=" * 78)
    print("验收结论 / ACCEPTANCE VERDICT: %s" % summary["verdict"])
    print("  检查项 : %d 通过 / %d 总计（失败 %d）"
          % (summary["checks_passed"], summary["checks_total"], summary["checks_failed"]))
    print("  章节数 : %d" % summary["chapters"])
    print("  Canon  : %d 张表 / %d 字段 / %d 索引"
          % (len(tables), n_cols, n_idx))
    print("  耗时   : %.1fs" % elapsed)
    print("  报告   : %s" % args.out)
    print("=" * 78)
    for r in failed:
        print("  FAIL %s：%s" % (r["name"], r.get("detail", "")))

    if not args.keep:
        shutil.rmtree(root, ignore_errors=True)
    else:
        print("  项目保留在：%s" % root)
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
