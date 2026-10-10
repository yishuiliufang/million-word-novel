#!/usr/bin/env python3
"""Million-word novel state machine (Long-Context Narrative OS).

    init / status / plan / next / audit / extract / commit / part / round-done
    recover / rollback / auto-next / validate / import / migrate

THE CONTRACT THAT CHANGED
-------------------------
The pre-upgrade `commit` wrote the chapter file and then asked you to audit it.
This one refuses to commit anything that has not already passed:

    draft -> prose validation -> semantic audit -> canon extraction
          -> canon consistency validation -> PASS -> commit -> snapshot -> index

and it performs the commit as a transaction (staging + journal + atomic rename +
one SQLite transaction). `round-done` no longer means `round += 1`: it verifies
the whole round and returns ROUND_BLOCKED (exit 3) when the project is not in a
legal state. Both changes are deliberate and are listed as breaking points in
docs/CHANGES.md.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import canon_db as db  # noqa: E402
import chapter_plan as cp  # noqa: E402
import context_builder as ctxb  # noqa: E402
import derived  # noqa: E402
import extract as extract_mod  # noqa: E402
import foreshadow as fs_mod  # noqa: E402
import llm as llm_mod  # noqa: E402
import narrative_audit as audit_mod  # noqa: E402
import tx as tx_mod  # noqa: E402
from _common import (  # noqa: E402
    COUNT_MODES,
    DEFAULT_CHAPTERS_PER_ROUND,
    DEFAULT_CHARS_PER_CHAPTER,
    DEFAULT_COUNT_MODE,
    DEFAULT_TARGET,
    DEFAULT_VOLUMES,
    NarrativeError,
    append_text,
    atomic_write_json,
    atomic_write_text,
    chapter_numbers,
    canonical_text,
    chapter_path,
    count_chars,
    die,
    ensure_dirs,
    jprint,
    load_progress,
    normalize_text,
    now_iso,
    parse_outline,
    paths,
    read_text,
    save_progress,
    sha256_text,
    staging_chapter_path,
    status_dict,
    tail_chars,
    tolerance,
    volume_bounds,
    volume_of,
    write_text,
)

CONTEXT_TEMPLATE = """# 上一轮收尾上下文

（尚未开写。第一轮请先建立设定圣经与总纲。）
"""

BANNED_TEMPLATE = """# 每行一条。命中即判不合格，必须重写该段。
不禁
不禁地
瞳孔骤缩
心中一惊
嘴角勾起
嘴角扬起
眼中闪过一丝
眼底闪过一丝
空气仿佛凝固
时间仿佛静止
不由自主地
鬼使神差
五味杂陈
百感交集
心中一沉
如遭雷击
宛如晴天霹雳
仿佛整个世界都
不知道过了多久
沉默良久
意味深长地
深吸一口气
缓缓开口
淡淡地说
冷冷地说
微微一笑
喃喃自语
眼神复杂
复杂的神色
无声地叹了口气
令人窒息
不寒而栗
毛骨悚然
一丝不易察觉的
不易察觉的
若有若无
若有似无
鬼魅
诡异莫名
说不出的
难以言喻
无法形容
总而言之
综上所述
值得一提的是
不得不说
毫无疑问
显然
事实上
"""

BIBLE_TEMPLATE = """# 设定圣经（Bible）

> 这是**人写的设定源文件**。它不是事实来源：事实来源是 `state/canon.db`。
> 使用方式：填好本文件后执行一次
>     python scripts/canon.py import-md --root "<项目目录>"
> 把内容灌进 Canon；之后 `state/bible.md` 会变成 canon.db 的自动派生视图。
> 一致性优先顺序：章节正文 > 校验过的 Canon Delta > canon.db > 派生摘要 > Markdown。

## 1. 一句话故事
{premise}

## 2. 主题与基调
- 主题：
- 基调：
- 叙述人称：
- 语言风格：

## 3. 世界规则（硬设定）
| 编号 | 规则 | 首次出现章节 | 不可违反 |
|---|---|---|---|
| R1 |  | 1 | 是 |

## 4. 主要人物
| 姓名 | 身份 | 核心欲望 | 致命缺陷 | 结局走向 | 状态 |
|---|---|---|---|---|---|
|  |  |  |  |  | 存活 |

## 5. 地理与组织
| 名称 | 类型 | 说明 |
|---|---|---|
|  |  |  |

## 6. 时间线
| 时间 | 事件 | 涉及章节 |
|---|---|---|
|  |  |  |

## 7. 伏笔登记表
| 编号 | 伏笔 | 埋设章节 | 计划回收章节 | 状态 |
|---|---|---|---|---|
| F1 |  | 1 | 60 | 未回收 |

## 8. 剧情线程
| 编号 | 名称 | 类型 | 目标 | 状态 |
|---|---|---|---|---|
| T1 |  | main |  | planned |

## 9. 已用尽的素材（避免重复）
- 已用意象：
- 已用桥段：
- 已用金句：
"""

OUTLINE_TEMPLATE = """# 分卷总纲

> 卷数 {volumes}，预估总章节 {estimated} 章，每章约 {chars} 字。
> **outline.md 是人写的计划视图，并且是 `next` 的一等输入**：`next` 会解析出
> 每一卷的目标与「卷末不可逆变化」，放进本章上下文的结构记忆层（L2）。
> 每卷结束时必须有一个不可逆的重大变化。**卷末必须有人再也回不去**。

"""

LEDGER_TEMPLATE = """<!-- AUTO-GENERATED from state/canon.db — 请勿手工编辑 -->
# 剧情台账（Plot Ledger）· 派生视图

> 本文件由 canon.db 自动重建。不要手工追加行。

| 章节 | 字数 | 时间 | 地点 | 本章事件 | 状态变化 | 伏笔变化 | 知识新增 |
|---|---|---|---|---|---|---|---|
"""


# ================================================================ helpers

def _open_conn(root: str):
    return db.connect(root)


def _require_canon(root: str):
    if not db.db_exists(root):
        die("state/canon.db 不存在：请运行 novel_state.py init 或 novel_state.py migrate --root %s"
            % root)
    return db.connect(root)


def _round_index(prog: dict, conn=None) -> int:
    if conn is not None:
        open_round = db.latest_open_round(conn)
        if open_round:
            return int(open_round["round_index"])
    return int(prog.get("rounds_completed", 0)) + 1


def _planned_chapters(prog: dict, st: dict, root: str) -> list:
    """Legacy round-sizing logic, unchanged (round_char_budget included)."""
    per_round = int(prog.get("chapters_per_round", DEFAULT_CHAPTERS_PER_ROUND))
    per_chapter = int(prog.get("chars_per_chapter", DEFAULT_CHARS_PER_CHAPTER))
    lo, hi = tolerance(prog)
    round_budget = int(prog.get("round_char_budget") or 40000)
    per_round = max(1, min(per_round, round_budget // max(1, per_chapter)))
    need = st["remaining_chars"]
    rounds_needed = max(1, -(-need // max(1, per_round * per_chapter)))
    count = per_round
    if rounds_needed == 1:
        count = max(1, -(-need // per_chapter))
    count = min(count, max(1, -(-need // max(1, lo)))) if need else 0
    start = st["next_chapter"]
    return list(range(start, start + count))


def _resync(root: str, conn, *, quiet: bool = True) -> dict:
    """Converge derived views + progress.json onto the database.

    This is the repair path used after crash recovery and rollback: the DB is the
    source of truth, so the Markdown views and the chapter counter are rebuilt
    from it rather than trusted.
    """
    prog = load_progress(root)
    prog["chapters_committed"] = len(chapter_numbers(root))
    prog["highest_chapter"] = max(chapter_numbers(root) or [0])
    save_progress(root, prog)
    os.makedirs(paths(root)["derived"], exist_ok=True)
    res = derived.write_views(root, conn)
    return {"derived": len(res["written"]), "notes": res["notes"],
            "chapters_committed": prog["chapters_committed"]}


def _outline_and_sync(root: str, conn, prog: dict) -> dict:
    outline = parse_outline(paths(root)["outline"])
    try:
        db.sync_outline(conn, outline, prog)
    except Exception:
        pass
    return outline


# ================================================================ init

def cmd_init(args) -> int:
    root = os.path.abspath(args.root)
    p = ensure_dirs(root)
    if os.path.isfile(p["progress"]) and not args.force:
        die("progress.json already exists at %s (use --force to reinitialize)" % root)

    est = max(1, int(round(args.target / float(args.chars_per_chapter))))
    prog = {
        "schema": 2,
        "title": args.title,
        "premise": args.premise or "",
        "root": root,
        "target_chars": int(args.target),
        "count_mode": args.count_mode,
        "chars_per_chapter": int(args.chars_per_chapter),
        "chars_per_chapter_min": int(args.chars_per_chapter * 0.85),
        "chars_per_chapter_tolerance": [int(args.chars_per_chapter * 0.8),
                                        int(args.chars_per_chapter * 1.25)],
        "chapters_per_round": int(args.chapters_per_round),
        "estimated_chapters": est,
        "volume_count": int(args.volumes),
        "volume_plan": [],
        "volumes": volume_bounds({"estimated_chapters": est, "volume_count": int(args.volumes)}),
        "round_char_budget": 40000,
        "part_chars": 3500,
        "snapshot_base_interval": 25,
        "auto_next": False,
        "llm_provider": args.llm_provider,
        "gate_policy": {"require_plan": True, "require_audit": True,
                        "require_canon_consistency": True},
        "rounds_completed": 0,
        "chapters_committed": 0,
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "loop_protocol": {
            "state_files": ["state/progress.json", "state/canon.db", "state/bible.md",
                            "state/outline.md", "state/plot-ledger.md", "state/last-context.md",
                            "state/derived/"],
            "resume_command": 'python scripts/novel_state.py next --root "%s"' % root,
            "stop_condition": "total_chars >= target_chars",
            "canonical_truth_order": ["chapter text", "validated canon delta", "canon.db",
                                      "derived summaries", "markdown views"],
        },
    }

    write_text(p["bible"], BIBLE_TEMPLATE.format(premise=args.premise or "（待补）"))
    write_text(p["outline"], OUTLINE_TEMPLATE.format(
        volumes=args.volumes, estimated=est, chars=args.chars_per_chapter))
    if not os.path.isfile(p["ledger"]):
        write_text(p["ledger"], LEDGER_TEMPLATE)
    if not os.path.isfile(p["last_context"]):
        write_text(p["last_context"], CONTEXT_TEMPLATE)
    if not os.path.isfile(p["banned"]):
        write_text(p["banned"], BANNED_TEMPLATE)

    blocks = []
    for b in prog["volumes"]:
        blocks.append(
            "## 第%d卷（第 %d–%d 章）\n\n"
            "- 卷目标（不可逆变化）：\n"
            "- 主要冲突：\n"
            "- 关键转折：\n"
            "- 卷末钩子：\n" % (b["volume"], b["start_chapter"], b["end_chapter"]))
    append_text(p["outline"], "\n".join(blocks) + "\n")

    save_progress(root, prog)
    conn = db.init_db(root)
    outline = _outline_and_sync(root, conn, prog)
    _resync(root, conn)
    conn.close()

    # Ship the skill's own references and test suite with the project, so a novel
    # project is self-contained: `python tests/run_tests.py` works inside it.
    skill_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for key, sub in (("references", "references"), ("tests", "tests")):
        src = os.path.join(skill_root, sub)
        dst = p[key]
        if os.path.isdir(src) and os.path.abspath(src) != os.path.abspath(dst):
            shutil.copytree(src, dst, dirs_exist_ok=True)

    jprint({
        "ok": True, "root": root, "estimated_chapters": est,
        "progress": p["progress"], "canon_db": p["canon"],
        "outline_volumes": len(outline.get("volumes") or []),
        "next_steps": [
            "1) 把 state/bible.md 填成真设定（规则/人物/伏笔/线程），然后运行："
            " python scripts/canon.py import-md --root \"%s\"" % root,
            "2) 把 state/outline.md 每卷填上「卷末不可逆变化」",
            "3) python scripts/novel_state.py next --root \"%s\"" % root,
        ],
    })
    return 0


# ================================================================ migrate

def cmd_migrate(args) -> int:
    """Upgrade a pre-upgrade project in place: schema 2 + Canon DB + snapshots.

    Nothing is deleted. Every existing chapter gets a canonical row and a
    snapshot so that rollback and validation work from chapter 1 onwards.
    """
    root = os.path.abspath(args.root)
    p = ensure_dirs(root)
    prog = load_progress(root)          # migrates schema 1 -> 2 in memory
    nums = chapter_numbers(root)
    mode = prog.get("count_mode", DEFAULT_COUNT_MODE)

    fresh = not db.db_exists(root)
    conn = db.init_db(root) if fresh else db.connect(root)
    outline = _outline_and_sync(root, conn, prog)
    import canon as canon_cli
    bible_info = canon_cli.import_bible_md(root, conn, actor="migrate")
    db.sync_outline(conn, outline, prog)

    created = 0
    if args.backfill_snapshots:
        for n in nums:
            if db.chapter_row(conn, n):
                continue
            text = canonical_text(read_text(chapter_path(root, n)))
            chars = count_chars(text, mode)
            sha = sha256_text(text)
            delta = {
                "schema": 1, "chapter": n, "source": "migrate",
                "summary": "（迁移导入的存量章节，未做 Canon 提取）",
                "events": [], "character_changes": [], "warnings": [
                    {"code": "MIGRATED_NO_EXTRACTION",
                     "message": "ch-%04d 为迁移导入，未做 Canon 提取" % n}],
            }
            conn.execute("BEGIN IMMEDIATE")
            report = db.apply_delta(conn, delta, commit_id="migrate-%04d" % n, actor="migrate")
            payload = db.build_snapshot(root, report, chapter=n, commit_id="migrate-%04d" % n,
                                        chapter_sha=sha, chars=chars,
                                        volume=volume_of(prog, n),
                                        summary=delta["summary"], title="",
                                        gates={"plan": "missing", "audit": "imported",
                                               "canon": "waived"})
            path = db.write_snapshot_file(root, payload)
            db.insert_chapter(conn, {
                "number": n, "chars": chars, "count_mode": mode, "sha256": sha,
                "volume": volume_of(prog, n), "commit_id": "migrate-%04d" % n,
                "audit_status": "imported", "gates": {"plan": "missing", "audit": "imported",
                                                      "canon": "waived"},
                "committed_at": now_iso()})
            db.insert_snapshot_row(conn, payload, path)
            db.set_gates(conn, n, {"audit": "imported", "canon": "waived"})
            conn.execute("COMMIT")
            created += 1

    prog["chapters_committed"] = len(nums)
    prog.setdefault("migrated_at", now_iso())
    save_progress(root, prog)
    res = _resync(root, conn)
    conn.close()
    jprint({"ok": True, "root": root, "schema": prog.get("schema"),
            "chapters_found": len(nums), "snapshots_created": created,
            "bible_import": bible_info, "derived": res,
            "outline_volumes": len(outline.get("volumes") or []),
            "next": "python scripts/validate_state.py --root \"%s\"" % root})
    return 0


# ================================================================ status

def cmd_status(args) -> int:
    st = status_dict(args.root)
    if db.db_exists(args.root):
        conn = db.connect(args.root)
        st["canon"] = {
            "characters": int(conn.execute(
                "SELECT COUNT(*) c FROM entities WHERE kind='character'").fetchone()["c"]),
            "events": int(conn.execute("SELECT COUNT(*) c FROM events").fetchone()["c"]),
            "open_foreshadowing": len([f for f in db.list_foreshadow(conn)
                                       if f["status"] not in fs_mod.TERMINAL]),
            "overdue_foreshadowing": len(fs_mod.overdue(db.list_foreshadow(conn),
                                                        st["highest_chapter"])),
            "threads_open": len([t for t in db.list_threads(conn)
                                 if t["status"] in ("planned", "active", "dormant")]),
            "snapshots": int(conn.execute(
                "SELECT COUNT(*) c FROM snapshots").fetchone()["c"]),
        }
        open_round = db.latest_open_round(conn)
        st["open_round"] = open_round["round_index"] if open_round else None
        conn.close()
    if args.json:
        jprint(st)
        return 0
    print("《%s》" % st["title"])
    print("  进度 : %s / %s 字（%.2f%%）" % (format(st["total_chars"], ","),
                                             format(st["target_chars"], ","), st["percent"]))
    print("  剩余 : %s 字" % format(st["remaining_chars"], ","))
    print("  章节 : 已提交 %d 章，下一章 ch-%04d" % (st["chapters_committed"], st["next_chapter"]))
    print("  当前 : 第 %d / %d 卷" % (st["current_volume"], st["volume_count"]))
    print("  轮次 : 已完成 %d 轮，预计还需 %d 轮" % (st["rounds_completed"],
                                                    st["estimated_rounds_left"]))
    if st.get("canon"):
        c = st["canon"]
        print("  Canon: 人物 %d｜事件 %d｜未回收伏笔 %d（逾期 %d）｜未解决线程 %d｜快照 %d"
              % (c["characters"], c["events"], c["open_foreshadowing"],
                 c["overdue_foreshadowing"], c["threads_open"], c["snapshots"]))
    if st["chapter_gaps"]:
        print("  警告 : 章节号不连续（缺 %s），运行 validate_state.py" % st["chapter_gaps"][:8])
    print("  状态 : %s" % ("[DONE] 已达标，可停止" if st["complete"] else "[LOOP] 未达标，继续轮回"))
    return 0


# ================================================================ next

def _ledger_tail(root: str, lines: int = 12) -> str:
    p = paths(root)["ledger"]
    if not os.path.isfile(p):
        return "（无台账）"
    rows = [ln for ln in read_text(p).splitlines() if ln.strip().startswith("|")]
    return "\n".join(rows[-lines:]) if rows else "（台账为空）"


def _build_brief(root: str, prog: dict, st: dict, conn) -> dict:
    outline = _outline_and_sync(root, conn, prog)
    round_index = _round_index(prog, conn)
    open_round = db.latest_open_round(conn)
    if open_round and int(open_round["round_index"]) == round_index:
        planned = list(open_round["planned_chapters"])
    else:
        planned = _planned_chapters(prog, st, root)
        db.start_round(conn, round_index, planned)
    committed = set(chapter_numbers(root))
    remaining = [c for c in planned if c not in committed]

    per_chapter = int(prog.get("chars_per_chapter", DEFAULT_CHARS_PER_CHAPTER))
    lo, hi = tolerance(prog)
    mode = prog.get("count_mode", DEFAULT_COUNT_MODE)
    start = remaining[0] if remaining else st["next_chapter"]

    plan_row = db.get_plan(conn, start, "chapter")
    plan = plan_row["payload"] if plan_row else None
    plan_file = os.path.join(paths(root)["plans"], "ch-%04d.plan.json" % start)

    query = " ".join([st.get("title") or "", (plan or {}).get("chapter_goal") or "",
                      (plan or {}).get("core_conflict") or ""])
    context = ctxb.build_context(root, conn, prog, start, plan=plan, query=query,
                                 outline=outline, status={"chars_tolerance": [lo, hi],
                                                          "chars_per_chapter": per_chapter})
    canon_digest = context["canon_digest"]

    draft_mode = "parts" if per_chapter >= 8000 else "single"
    part_chars = int(prog.get("part_chars") or 3500)
    parts_needed = max(1, -(-per_chapter // max(1, part_chars))) if draft_mode == "parts" else 1
    recovery = tx_mod.pending_work(root, conn)

    action = "WRITE"
    if not remaining:
        action = "ROUND_DONE"
    elif st["complete"]:
        action = "STOP"

    brief = {
        "action": action,
        "round_index": round_index,
        "round_planned_chapters": planned,
        "round_remaining_chapters": remaining,
        "status": st,
        "volume": volume_of(prog, start),
        "volume_range": next((b for b in volume_bounds(prog) if b["volume"] ==
                              volume_of(prog, start)), None),
        "chapters_to_write": remaining if action == "WRITE" else [],
        "chars_per_chapter": per_chapter,
        "chars_tolerance": [lo, hi],
        "count_mode": mode,
        "draft_mode": draft_mode,
        "part_chars": part_chars,
        "parts_per_chapter": parts_needed,
        "char_rule": "每章按 %s 口径统计，落在 %d–%d 字之间；不足重写，超出拆章。" % (mode, lo, hi),

        # ---- compatibility keys (pre-upgrade consumers read these) ----
        "bible_digest": canon_digest,
        "ledger_tail": _ledger_tail(root),
        "previous_chapter_tail": context["L0_current"]["previous_chapter_tail"],
        "last_context": context["L0_current"]["last_context"],
        "commit_command": 'python scripts/novel_state.py commit --root "%s" '
                          '--chapter <N> --file <草稿路径>' % root,
        "after_round": 'python scripts/novel_state.py round-done --root "%s"' % root,

        # ---- new: four-layer memory + canon ----
        "memory_layers": ["L0_current", "L1_local", "L2_structure", "L3_canon"],
        "context": context,
        "canon_digest": canon_digest,
        "entities": context["entities"],
        "character_states": context["L3_canon"]["character_states"],
        "relevant_foreshadowing": context["relevant_foreshadowing"],
        "outline_participation": {
            "parsed": bool(outline.get("parsed")),
            "volumes": len(outline.get("volumes") or []),
            "issues": outline.get("issues") or [],
        },
        "volume_goal": context["L2_structure"]["volume_goal"],
        "volume_irreversible_change": context["L2_structure"]["volume_irreversible_change"],
        "open_foreshadowing": context["L2_structure"]["open_foreshadowing"],
        "unresolved_threads": context["L1_local"]["unresolved_threads"],

        # ---- new: planning workflow ----
        "workflow": ["plan", "write", "audit", "extract", "commit"],
        "plan_required": plan is None,
        "plan_file": plan_file,
        "plan_digest": cp.plan_digest(plan) if plan else "",
        "plan_command": 'python scripts/novel_state.py plan --root "%s" --chapter %d '
                        '--file <计划JSON路径> --check' % (root, start),
        "plan_skeleton_command": 'python scripts/novel_state.py plan --root "%s" '
                                 '--chapter %d --out "%s"' % (root, start, plan_file),
        "scene_plan_command": 'python scripts/novel_state.py scene-plan --root "%s" '
                              '--chapter %d' % (root, start),
        "audit_command": 'python scripts/novel_state.py audit --root "%s" --chapter %d '
                         '--file <草稿路径> --strict' % (root, start),
        "delta_file": extract_mod.delta_path_for(root, start),
        "extract_command": 'python scripts/novel_state.py extract --root "%s" --chapter %d '
                           '--file <草稿路径> --out "%s"'
                           % (root, start, extract_mod.delta_path_for(root, start)),
        "commit_requires": ["plan", "audit(passing, same text hash)", "canon delta valid"],

        "recovery": {"needs_recover": recovery["needs_recover"],
                     "open_transactions": recovery["open_transactions"],
                     "uncommitted_drafts": [d["chapter"] for d in recovery["uncommitted_drafts"]],
                     "resume": recovery["resume"]},
        "canon_db": paths(root)["canon"],
        "hard_rules": [
            "只写 brief 里列出的章节，一章一个文件，写完立刻 commit，不要攒着一起提交。",
            "**先 plan 再写**：本章没有通过校验的 chapter plan 时，commit 会拒绝。",
            "**先 audit 再 commit**：commit 只接受针对当前草稿哈希的通过审计；改稿后必须重审。",
            "Canon 只能通过校验过的 Canon Delta 写入：不要手改 state/bible.md 当作事实。",
            "任何不确定的判断都不要写进 Canon，标 uncertain 让它变成 warning。",
            "每章必须推进主线；禁止无冲突的过场章、禁止总结式收尾。",
            "禁止复述前文摘要；上一章交代过的信息不许再解释一遍。",
            "命中 banned-phrases.txt 的句子必须重写。",
            "本轮结束时执行 round-done；若返回 ROUND_BLOCKED，先修复再收轮。",
        ],
    }

    if action == "ROUND_DONE":
        brief["instruction"] = ("本轮计划章节已全部提交。运行 round-done 收轮：%s"
                                % brief["after_round"])
    if draft_mode == "parts":
        brief["part_command"] = ('python scripts/novel_state.py part --root "%s" '
                                 '--chapter <N> --file <本节草稿路径>' % root)
        brief["part_check"] = ('python scripts/novel_state.py part --root "%s" '
                               '--chapter <N> --check' % root)
        brief["draft_instruction"] = (
            "本章 %d 字，一次输出写不完。必须分 %d 节来写：每节 %d–%d 字，"
            "每节写成独立文件后用 part 命令追加到同一章的 staging 文件，"
            "然后用 part --check 看累计字数。累计达到 %d 字后再用 commit 提交整章。"
            "每节之间必须推进情节（换场景/换冲突/换时间点），"
            "禁止把同一段内容换句话重写一遍凑字数。"
            % (per_chapter, parts_needed, int(part_chars * 0.85), int(part_chars * 1.2), lo))
        brief["hard_rules"] = [
            "本章是长章（%d 字），必须分节写、分节追加，禁止试图一次性写完。" % per_chapter,
            "每一节都要有新的事件推进；重复上一节的内容视为失败。",
            "每节写完后立刻 part 追加，不要攒着。",
            "累计字数达到下限后立即 commit 整章，不要为了凑上限硬拖。",
        ] + brief["hard_rules"]
    return brief


def cmd_next(args) -> int:
    root = args.root
    prog = load_progress(root)
    if db.db_exists(root):
        conn = _open_conn(root)
        if not args.no_recover:
            rec = tx_mod.recover(root, conn, apply=True)
            if rec["found"]:
                _resync(root, conn)
        st = status_dict(root)
        if st["complete"]:
            brief = {"action": "STOP", "reason": "target reached", "status": st,
                     "instruction": "全书已达目标字数，运行 export 后停止轮回，不要再写新章。"}
            conn.close()
            jprint(brief)
            return 0
        brief = _build_brief(root, prog, st, conn)
        conn.close()
    else:
        # Legacy project without Canon: keep the old behaviour alive but say so.
        st = status_dict(root)
        if st["complete"]:
            jprint({"action": "STOP", "reason": "target reached", "status": st})
            return 0
        planned = _planned_chapters(prog, st, root)
        brief = {
            "action": "WRITE", "status": st, "chapters_to_write": planned,
            "chars_per_chapter": prog.get("chars_per_chapter"),
            "chars_tolerance": list(tolerance(prog)),
            "bible_digest": read_text(paths(root)["bible"])[:4000]
            if os.path.isfile(paths(root)["bible"]) else "",
            "ledger_tail": _ledger_tail(root),
            "warning": "state/canon.db 不存在：当前处于兼容模式（截断式 Markdown 记忆）。"
                       "运行 novel_state.py migrate 升级为 Canon 记忆系统。",
        }
    if args.json_output is False:
        pass
    jprint(brief)
    return 0


# ================================================================ plan

def cmd_plan(args) -> int:
    """Two modes, one command (backwards compatible).

    Legacy:  --volume-plan / --chars-per-chapter / --chapters-per-round / --target
             set book-wide structure.
    New:     --chapter N [--file plan.json] [--out file] [--check]
             produce + validate a Chapter Plan (required before writing).
    """
    root = args.root
    prog = load_progress(root)

    legacy = any([args.volume_plan, args.chars_per_chapter, args.chapters_per_round,
                  args.target, args.estimated_chapters])
    if legacy:
        if args.volume_plan:
            plan = [int(x) for x in args.volume_plan.replace(" ", "").split(",") if x]
            if not plan or any(c <= 0 for c in plan):
                die("--volume-plan must be a comma list of positive chapter counts")
            prog["volume_plan"] = plan
            prog["volume_count"] = len(plan)
        if args.chars_per_chapter:
            prog["chars_per_chapter"] = int(args.chars_per_chapter)
            prog["chars_per_chapter_min"] = int(args.chars_per_chapter * 0.85)
            prog["chars_per_chapter_tolerance"] = [int(args.chars_per_chapter * 0.8),
                                                   int(args.chars_per_chapter * 1.25)]
        if args.chapters_per_round:
            prog["chapters_per_round"] = int(args.chapters_per_round)
        if args.target:
            prog["target_chars"] = int(args.target)
        if args.estimated_chapters:
            prog["estimated_chapters"] = int(args.estimated_chapters)
        prog["volumes"] = volume_bounds(prog)
        save_progress(root, prog)
        if db.db_exists(root):
            conn = db.connect(root)
            outline = parse_outline(paths(root)["outline"])
            db.sync_outline(conn, outline, prog)
            conn.close()
        jprint({"ok": True, "mode": "structure", "volumes": prog["volumes"],
                "chars_per_chapter": prog.get("chars_per_chapter"),
                "chapters_per_round": prog.get("chapters_per_round"),
                "target_chars": prog.get("target_chars")})
        if not args.chapter:
            return 0

    if not args.chapter:
        die("plan needs --chapter N (chapter planning) or --volume-plan/--chars-per-chapter "
            "(structure planning)")

    if not db.db_exists(root):
        die("state/canon.db 不存在：运行 novel_state.py init 或 migrate")
    conn = db.connect(root)
    chapter = int(args.chapter)
    st = status_dict(root)
    outline = _outline_and_sync(root, conn, prog)
    existing = db.get_plan(conn, chapter, "chapter")
    plan = existing["payload"] if existing else None

    if args.file:
        supplied = cp.load_plan_file(args.file)
        base = plan or cp.skeleton(conn, prog, chapter,
                                   ctxb.build_context(root, conn, prog, chapter, outline=outline),
                                   outline)
        merged = dict(base)
        merged.update(supplied)
        merged["source"] = "agent"
        plan = merged
    if plan is None:
        context = ctxb.build_context(root, conn, prog, chapter, outline=outline,
                                     status={"chars_tolerance": list(tolerance(prog)),
                                             "chars_per_chapter": prog.get("chars_per_chapter")})
        plan = cp.skeleton(conn, prog, chapter, context, outline)

    context = ctxb.build_context(root, conn, prog, chapter, plan=plan, outline=outline)
    plan, default_warnings = cp.default_fill(plan, context)
    verdict = cp.validate_plan(plan, context, ctxb.canon_view(conn, prog, outline), chapter)

    out_path = args.out or os.path.join(paths(root)["plans"], "ch-%04d.plan.json" % chapter)
    plan["validated_at"] = now_iso()
    plan["validation"] = {"ok": verdict["ok"], "errors": verdict["errors"]}
    if args.check and not verdict["ok"]:
        jprint({"ok": False, "mode": "chapter", "chapter": chapter,
                "errors": verdict["errors"], "warnings": verdict["warnings"] + default_warnings})
        conn.close()
        return 2

    atomic_write_json(out_path, plan)
    pid = db.save_plan(conn, chapter, plan, kind="chapter",
                       status="approved" if verdict["ok"] else "draft",
                       source=plan.get("source") or "rule")
    db.set_gates(conn, chapter, {"plan": "pass" if verdict["ok"] else "fail"})
    for w in (verdict["warnings"] + default_warnings):
        db.add_warning(conn, w.get("code") or "PLAN_WARNING", w.get("message") or "",
                       chapter=chapter)
    conn.close()

    jprint({"ok": verdict["ok"], "mode": "chapter", "chapter": chapter,
            "plan_id": pid, "plan_file": out_path,
            "errors": verdict["errors"],
            "warnings": default_warnings + verdict["warnings"],
            "digest": cp.plan_digest(plan),
            "scenes": len(plan.get("scenes") or []),
            "next": "python scripts/novel_state.py scene-plan --root \"%s\" --chapter %d"
                    % (root, chapter)})
    return 0 if verdict["ok"] else 2


def cmd_scene_plan(args) -> int:
    root = args.root
    if not db.db_exists(root):
        die("state/canon.db 不存在")
    conn = db.connect(root)
    row = db.get_plan(conn, int(args.chapter), "chapter")
    if row is None:
        conn.close()
        die("ch-%04d 还没有 chapter plan：先运行 plan --chapter %d" % (args.chapter, args.chapter))
    scenes = cp.scene_plan(row["payload"])
    conn.close()
    if args.json:
        jprint({"chapter": args.chapter, "scenes": scenes})
    else:
        print("第 %s 章 Scene Plan（%d 场）" % (args.chapter, len(scenes)))
        for sc in scenes:
            print("  [%d/%d] @%s  参与：%s" % (sc["scene_no"], sc["of"], sc["location"],
                                              "、".join(sc["participants"])))
            print("        冲突：%s" % sc["conflict"])
            print("        目标：%s" % sc["goal"])
            if sc["summary"]:
                print("        梗概：%s" % sc["summary"])
            if sc["must_not"]:
                print("        禁止：%s" % "、".join(sc["must_not"]))
    return 0


# ================================================================ extract

def _obtain_delta(root, chapter: int, text: str, *, plan: dict, conn, args) -> tuple:
    """Return (delta, warnings, provider). Explicit delta wins over LLM over rules."""
    prog = load_progress(root)
    outline = parse_outline(paths(root)["outline"])
    canon = ctxb.canon_view(conn, prog, outline) if conn is not None else {}
    warnings = []

    path = args.delta or extract_mod.discover_delta(root, chapter)
    if path:
        delta = extract_mod.load_delta(path)
        delta.setdefault("source", "delta")
        return delta, warnings, "delta:%s" % os.path.basename(path)

    spec = args.llm or prog.get("llm_provider") or "rule"
    if spec not in ("rule", "none", ""):
        plan = plan or {}
        prompt = extract_mod.build_prompt(
            title=prog.get("title") or "", chapter=chapter, text=text, canon_view=canon,
            plan=plan, timeline_last=canon.get("timeline_last") or "",
            forbidden=plan.get("forbidden_reveals") or [])
        try:
            delta = llm_mod.run_json(spec, prompt, timeout=args.timeout)
            delta.setdefault("source", "llm")
            return delta, warnings, spec
        except NarrativeError as exc:
            warnings.append({"code": "LLM_EXTRACT_FAILED",
                             "message": "LLM 提取失败，回退到规则提取：%s" % exc})
    delta = extract_mod.rule_extract(chapter, text, plan=plan, canon=canon,
                                     state_source=args.state_source)
    return delta, warnings, "rule"


def cmd_extract(args) -> int:
    """chapter -> structured Canon Delta (JSON). Never writes Canon."""
    root = args.root
    if not db.db_exists(root):
        die("state/canon.db 不存在")
    conn = db.connect(root)
    chapter = int(args.chapter)
    src = args.file or chapter_path(root, chapter)
    if not os.path.isfile(src):
        conn.close()
        die("正文/草稿文件不存在：%s" % src)
    text = read_text(src)
    plan_row = db.get_plan(conn, chapter, "chapter")
    plan = plan_row["payload"] if plan_row else None
    delta, warns, provider = _obtain_delta(root, chapter, text, plan=plan, conn=conn, args=args)
    outline = parse_outline(paths(root)["outline"])
    canon = ctxb.canon_view(conn, prog=load_progress(root), outline=outline)
    verdict = extract_mod.validate_delta(delta, chapter=chapter, canon=canon)
    delta = verdict["delta"]
    conn.close()

    if args.out:
        atomic_write_json(args.out, delta)
    if args.json or not args.out:
        out = {"ok": verdict["ok"], "provider": provider, "chapter": chapter,
               "delta": delta, "errors": verdict["errors"],
               "warnings": verdict["warnings"] + warns}
        jprint(out)
    else:
        jprint({"ok": verdict["ok"], "provider": provider, "out": args.out,
                "errors": verdict["errors"], "warnings": len(verdict["warnings"])})
    return 0 if verdict["ok"] else 2


# ================================================================ audit

def cmd_audit(args) -> int:
    root = args.root
    chapter = int(args.chapter)
    if not db.db_exists(root):
        die("state/canon.db 不存在（运行 init/migrate）")
    conn = db.connect(root)
    plan_row = db.get_plan(conn, chapter, "chapter")
    plan = plan_row["payload"] if plan_row else None
    delta = None
    if args.delta:
        delta = extract_mod.load_delta(args.delta)
    else:
        discovered = extract_mod.discover_delta(root, chapter)
        if discovered:
            delta = extract_mod.load_delta(discovered)
    prog = load_progress(root)
    outline = parse_outline(paths(root)["outline"])
    context = ctxb.build_context(root, conn, prog, chapter, plan=plan, outline=outline)
    result = audit_mod.audit_chapter(
        root, chapter, file=args.file or None, delta=delta, plan=plan,
        levels=(1, 2), llm_spec=args.llm, conn=conn,
        llm_canon_digest=context.get("canon_digest") or "",
        llm_plan_digest=cp.plan_digest(plan) if plan else "")
    report_path = os.path.join(paths(root)["audits"], "ch-%04d.json" % chapter)
    atomic_write_json(report_path, result)
    db.set_gates(conn, chapter, {"audit": "pass" if result["pass"] else "fail"})
    conn.close()
    result["report_path"] = report_path
    if args.json:
        jprint(result)
    else:
        print(audit_mod.render_report(result))
        print("报告：%s" % report_path)
    if args.strict and not result["pass"]:
        return 1
    return 0


# ================================================================ commit

def _gate_or_die(name: str, ok: bool, allow: bool, detail: str, waivers: list) -> str:
    if ok:
        return "pass"
    if allow:
        waivers.append({"gate": name, "detail": detail})
        return "waived"
    die("commit 被拒绝（%s 门）：%s\n"
        "  修复后重试；如确需越过，请显式使用 --allow-%s（会写入 warning 并被 round-done 报告）"
        % (name, detail, name.replace("_", "-")), 2)


def cmd_commit(args) -> int:
    root = args.root
    if not db.db_exists(root):
        die("state/canon.db 不存在：运行 novel_state.py init 或 migrate")
    conn = db.connect(root)
    tx_mod.recover(root, conn, apply=True)

    prog = load_progress(root)
    p = paths(root)
    mode = prog.get("count_mode", DEFAULT_COUNT_MODE)
    lo, hi = tolerance(prog)
    chapter = int(args.chapter)
    waivers = []

    src = os.path.abspath(args.file) if args.file else staging_chapter_path(root, chapter)
    if not os.path.isfile(src):
        conn.close()
        die("draft file not found: %s" % src)
    raw = read_text(src)
    text = canonical_text(raw)
    chars = count_chars(text, mode)
    sha = sha256_text(text)

    existing = chapter_numbers(root)
    if chapter in existing and not args.overwrite:
        conn.close()
        die("chapter %d already committed (use --overwrite to replace)" % chapter)
    expected_max = (max(existing) + 1) if existing else 1
    if chapter > expected_max:
        conn.close()
        die("cannot skip ahead: chapter %d, expected <= %d" % (chapter, expected_max))

    # ---- gate 1: length (deterministic)
    length_ok = (lo <= chars <= hi) or args.allow_short or args.allow_long
    if not length_ok:
        conn.close()
        die("chapter too short/long: %d not in [%d, %d] (%s). 补写场景或拆章后重交。"
            % (chars, lo, hi, mode))
    length_status = "pass" if lo <= chars <= hi else "waived"
    if length_status == "waived":
        waivers.append({"gate": "length", "detail": "%d 字不在 [%d,%d]" % (chars, lo, hi)})

    # ---- gate 2: plan
    plan_row = db.get_plan(conn, chapter, "chapter")
    plan = plan_row["payload"] if plan_row else None
    plan_ok = plan is not None
    plan_detail = ("ch-%04d 没有通过校验的 chapter plan。先运行：python scripts/novel_state.py "
                   "plan --root \"%s\" --chapter %d" % (chapter, root, chapter))
    plan_status = _gate_or_die("plan", plan_ok, args.allow_plan, plan_detail, waivers)

    # ---- gate 3: audit for THIS text hash (this is what enforces audit -> commit)
    a1 = db.audit_for(conn, chapter, sha, level=1)
    a2 = db.audit_for(conn, chapter, sha, level=2)
    audit_ok = bool(a1 and a1["pass"] and a2 and a2["pass"])
    if not audit_ok:
        if a1 is None and a2 is None:
            why = "没有针对当前文本（sha256=%s…）的审计记录" % sha[:12]
        elif (a1 and not a1["pass"]) or (a2 and not a2["pass"]):
            why = "审计未通过：%s" % ((a2 or a1 or {}).get("blocking") or [])[:2]
        else:
            why = "只完成了部分层级的审计（Level1=%s, Level2=%s）" % (bool(a1), bool(a2))
        audit_detail = ("%s。请运行：python scripts/novel_state.py audit --root \"%s\" "
                        "--chapter %d --file \"%s\" --strict" % (why, root, chapter, src))
    else:
        audit_detail = ""
    audit_status = _gate_or_die("audit", audit_ok, args.allow_audit, audit_detail, waivers)

    # ---- gate 4: canon delta
    delta, delta_warnings, provider = _obtain_delta(root, chapter, text, plan=plan,
                                                    conn=conn, args=args)
    outline = parse_outline(p["outline"])
    canon = ctxb.canon_view(conn, prog, outline)
    verdict = extract_mod.validate_delta(delta, chapter=chapter, canon=canon)
    delta = verdict["delta"]
    canon_errors = list(verdict["errors"])
    if plan:
        diff = cp.diff_plan_vs_delta(plan, delta)
        for m in diff["missing_state_changes"]:
            canon_errors.append("计划要求 %s.%s→%r，但 Canon Delta 中没有这次变化"
                                % (m["character"], m["field"], m["expected"]))
        for m in diff["mismatched_state_changes"]:
            canon_errors.append("计划要求 %s.%s=%r，但 Delta 给出 %r"
                                % (m["character"], m["field"], m["expected"], m["actual"]))
        for t in diff["untouched_threads"]:
            delta_warnings.append({"code": "PLANNED_THREAD_UNTOUCHED",
                                   "message": "计划要推进线程 %s，Delta 中没有其变化" % t})
    canon_ok = not canon_errors
    canon_status = _gate_or_die("canon", canon_ok, args.allow_canon,
                                "Canon 提取/一致性校验失败：%s" % canon_errors[:4], waivers)

    gates = {"plan": plan_status, "audit": audit_status, "canon": canon_status,
             "length": length_status}
    # ---- the transaction
    txn = tx_mod.CommitTransaction(root, chapter)
    try:
        txn.write_text("chapter", chapter_path(root, chapter), text)
        db.tx_row(conn, txn.id, chapter, "prepared", tx_mod.journal_path(root, txn.id))
        volume = volume_of(prog, chapter)
        tl = delta.get("timeline") or {}
        conn.execute("BEGIN IMMEDIATE")
        report = db.apply_delta(conn, delta, commit_id=txn.id, actor="commit")
        payload = db.build_snapshot(
            root, report, chapter=chapter, commit_id=txn.id, chapter_sha=sha, chars=chars,
            volume=volume, arc=delta.get("arc"),
            timeline=tl, summary=delta.get("summary") or extract_mod.human_summary(delta),
            title=delta.get("title") or "", gates=gates,
            plan_id=(plan_row or {}).get("id"))
        snap_path = db.snapshot_path(root, chapter)
        txn.write_text("snapshot", snap_path,
                       json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        db.insert_chapter(conn, {
            "number": chapter, "title": delta.get("title") or "", "chars": chars,
            "count_mode": mode, "sha256": sha, "volume": volume, "arc": delta.get("arc"),
            "round_index": _round_index(prog, conn),
            "timeline_at": tl.get("world_time"), "plan_id": (plan_row or {}).get("id"),
            "audit_status": "pass" if audit_ok else "waived",
            "gates": gates, "commit_id": txn.id, "committed_at": now_iso()})
        db.insert_snapshot_row(conn, payload, snap_path)
        db.set_gates(conn, chapter, gates)
        db.log_change(conn, "chapter_commit", actor=args.actor, entity_kind="chapter",
                      entity_ref="ch-%04d" % chapter, after={"sha256": sha, "chars": chars},
                      reason=args.reason or "正常提交", source=provider, chapter=chapter,
                      commit_id=txn.id)
        for w in (delta.get("warnings") or []) + delta_warnings:
            db.add_warning(conn, w.get("code") or "COMMIT_WARNING", w.get("message") or "",
                           chapter=chapter, data=w.get("data"))
        for w in waivers:
            db.add_warning(conn, "GATE_WAIVED",
                           "提交时越过了 %s 门：%s" % (w["gate"], w["detail"]),
                           chapter=chapter, severity="warning")
        db.round_task(conn, _round_index(prog, conn), chapter, "committed", "done")
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        txn.rollback_files()
        shutil.rmtree(txn.dir, ignore_errors=True)
        db.tx_row(conn, txn.id, chapter, "rolled_back",
                  tx_mod.journal_path(root, txn.id), steps=["db rollback"])
        conn.close()
        raise

    txn.mark_db_committed()
    # The database has committed. From here on the only correct outcome is to
    # publish the staged files; if anything fails the journal stays in the
    # db_committed state and `recover` completes publication later. Never call
    # finish() here: that would delete the journal and orphan the committed canon.
    try:
        views = derived.views_payload(root, conn)
        for path, content in views.items():
            txn.write_text("view:" + os.path.basename(path), path, content)
        prog["chapters_committed"] = len(set(existing) | {chapter})
        prog["highest_chapter"] = max(set(existing) | {chapter})
        prog["last_commit_at"] = now_iso()
        prog["last_chapter"] = chapter
        txn.write_text("progress", p["progress"],
                       json.dumps({**prog, "updated_at": now_iso()},
                                  ensure_ascii=False, indent=2) + "\n")
        txn.publish()
    except Exception:
        db.tx_row(conn, txn.id, chapter, "db_committed",
                  tx_mod.journal_path(root, txn.id), steps=["publish failed"])
        conn.close()
        raise
    txn.finish()
    # Archive the source draft so that "an uncommitted draft exists" keeps meaning
    # exactly that. Without this, every committed chapter leaves a draft behind and
    # the recovery manager cannot distinguish work-in-progress from finished work.
    archived = None
    draft_dir = paths(root)["drafts"]
    if os.path.dirname(os.path.abspath(src)) == os.path.abspath(draft_dir) and \
            os.path.basename(src) == "ch-%04d.md" % chapter:
        dst_dir = os.path.join(draft_dir, "committed")
        os.makedirs(dst_dir, exist_ok=True)
        archived = os.path.join(dst_dir, os.path.basename(src))
        try:
            os.replace(src, archived)
        except OSError:
            archived = None

    db.tx_row(conn, txn.id, chapter, "committed", tx_mod.journal_path(root, txn.id))
    interval = int(prog.get("snapshot_base_interval") or 25)
    if interval > 0 and chapter % interval == 0:
        try:
            db.write_base_snapshot(root, conn, chapter)
        except Exception:
            pass

    st = status_dict(root)
    open_fs = fs_mod.overdue(db.list_foreshadow(conn), chapter)
    conn.close()
    jprint({
        "ok": True, "chapter": chapter, "chars": chars, "mode": mode,
        "sha256": sha, "commit_id": txn.id,
        "gates": gates, "waivers": waivers,
        "delta_provider": provider,
        "extraction_warnings": len(delta.get("warnings") or []) + len(delta_warnings),
        "canon": {
            "entities_created": len(report.get("entities_created") or []),
            "events": len(report.get("events") or []),
            "state_changes": len(report.get("character_changes") or []),
            "knowledge_added": len(report.get("knowledge_added") or []),
            "foreshadow_changes": len(report.get("foreshadow_changes") or []),
            "thread_changes": len(report.get("thread_changes") or []),
        },
        "snapshot": snap_path,
        "draft_archived": archived,
        "overdue_foreshadowing": [f["code"] for f in open_fs],
        "total_chars": st["total_chars"], "remaining_chars": st["remaining_chars"],
        "percent": st["percent"], "complete": st["complete"],
        "summary": extract_mod.human_summary({**delta,
                                             "character_changes": [
                                                 {"character": c["character"], "field": c["field"],
                                                  "before": c["before"], "after": c["after"]}
                                                 for c in report.get("character_changes") or []],
                                             "foreshadow_changes": report.get("foreshadow_changes") or []}),
    })
    return 0


# ================================================================ import (legacy)

def cmd_import(args) -> int:
    root = args.root
    prog = load_progress(root)
    p = ensure_dirs(root)
    mode = prog.get("count_mode", DEFAULT_COUNT_MODE)

    files = []
    for pattern in args.group:
        hits = sorted(glob.glob(pattern))
        if not hits:
            die("no files matched: %s" % pattern)
        files.extend(hits)
    if not files:
        die("nothing to import (pass --group <glob>, repeatable, in chapter order)")

    start = args.start_chapter
    existing = chapter_numbers(root)
    if existing and start <= max(existing) and not args.overwrite:
        die("chapter %d.. already exist; use --overwrite or --start-chapter" % start)

    conn = db.connect(root) if db.db_exists(root) else None
    imported = []
    for i, src in enumerate(files):
        if args.limit and i >= args.limit:
            break
        n = start + i
        text = read_text(src).strip() + "\n"
        text = canonical_text(text)
        write_text(chapter_path(root, n), text)
        chars = count_chars(text, mode)
        title = ""
        for ln in text.splitlines():
            ln = ln.strip()
            if ln:
                title = ln.lstrip("# ").strip()
                break
        summary = (args.summary_prefix + " " + title).strip() if args.summary_prefix else title
        if conn is not None:
            sha = sha256_text(text)
            delta = {"schema": 1, "chapter": n, "source": "import", "title": title,
                     "summary": summary or "（导入）", "events": [],
                     "character_changes": [],
                     "warnings": [{"code": "IMPORTED_NO_EXTRACTION",
                                   "message": "ch-%04d 由 import 导入，未做 Canon 提取" % n}]}
            conn.execute("BEGIN IMMEDIATE")
            report = db.apply_delta(conn, delta, commit_id="import-%04d" % n, actor="import")
            payload = db.build_snapshot(root, report, chapter=n, commit_id="import-%04d" % n,
                                        chapter_sha=sha, chars=chars,
                                        volume=volume_of(prog, n), summary=delta["summary"],
                                        title=title,
                                        gates={"plan": "missing", "audit": "imported",
                                               "canon": "waived"})
            path = db.write_snapshot_file(root, payload)
            db.insert_chapter(conn, {"number": n, "title": title, "chars": chars,
                                     "count_mode": mode, "sha256": sha,
                                     "volume": volume_of(prog, n),
                                     "commit_id": "import-%04d" % n,
                                     "audit_status": "imported",
                                     "gates": {"plan": "missing", "audit": "imported",
                                               "canon": "waived"},
                                     "committed_at": now_iso()})
            db.insert_snapshot_row(conn, payload, path)
            db.set_gates(conn, n, {"audit": "imported", "canon": "waived"})
            db.add_warning(conn, "IMPORTED_NO_EXTRACTION",
                           "ch-%04d 为导入章节，未做 Canon 提取；建议之后补 extract"
                           % n, chapter=n)
            conn.execute("COMMIT")
        imported.append({"chapter": n, "source": os.path.basename(src), "chars": chars})

    prog["chapters_committed"] = len(chapter_numbers(root))
    save_progress(root, prog)
    if conn is not None:
        _resync(root, conn)
        conn.close()
    st = status_dict(root)
    jprint({
        "ok": True, "imported": len(imported),
        "first": imported[0]["chapter"] if imported else None,
        "last": imported[-1]["chapter"] if imported else None,
        "imported_chars": sum(r["chars"] for r in imported),
        "total_chars": st["total_chars"], "remaining_chars": st["remaining_chars"],
        "next_chapter": st["next_chapter"], "complete": st["complete"],
        "note": "导入章节绕过字数门（旧稿长度是历史事实），但没有 Canon 提取；"
                "运行 extract --chapter N 补齐。",
    })
    return 0


# ================================================================ part (long chapters)

def staging_path(root: str, number: int) -> str:
    return staging_chapter_path(root, number)


def cmd_part(args) -> int:
    root = args.root
    prog = load_progress(root)
    mode = prog.get("count_mode", DEFAULT_COUNT_MODE)
    number = int(args.chapter)
    lo, hi = tolerance(prog)

    existing_chapters = chapter_numbers(root)
    if number in existing_chapters and not args.overwrite:
        die("chapter %d already committed; parts cannot be appended to a finished chapter" % number)

    staging = staging_path(root, number)
    existing = read_text(staging) if os.path.isfile(staging) else ""

    if args.check:
        total = count_chars(existing, mode)
        jprint({"chapter": number, "staging": staging, "chars": total,
                "target_range": [lo, hi], "remaining_to_min": max(0, lo - total),
                "ready_to_commit": total >= lo, "over_limit": total > hi})
        return 0

    if not args.file:
        die("--file is required unless --check is used")
    src = os.path.abspath(args.file)
    if not os.path.isfile(src):
        die("part file not found: %s" % src)
    piece = read_text(src).strip()
    if not piece:
        die("part file is empty: %s" % src)

    merged = (existing.rstrip() + "\n\n" + piece + "\n") if existing.strip() else (piece + "\n")
    atomic_write_text(staging, merged)

    total = count_chars(merged, mode)
    part_chars = count_chars(piece, mode)
    jprint({"ok": True, "chapter": number, "part_chars": part_chars, "chars": total,
            "target_range": [lo, hi],
            "percent_of_chapter": round(total * 100.0 / hi, 1) if hi else 0,
            "remaining_to_min": max(0, lo - total),
            "ready_to_commit": lo <= total <= hi, "over_limit": total > hi,
            "staging": staging,
            "next": ('commit --chapter %d --file "%s"' % (number, staging)) if total >= lo
                    else "继续追加下一节（还差 %d 字）" % max(0, lo - total)})
    return 0


# ================================================================ round-done

def _infer_legacy_round(conn, committed: list, prog: dict) -> list:
    """A legacy project calls round-done without ever having started a round.
    Infer the round as "committed chapters that no round has claimed yet"."""
    claimed = set()
    for r in conn.execute("SELECT chapter FROM round_tasks WHERE stage='committed'").fetchall():
        claimed.add(r["chapter"])
    pending = [c for c in committed if c not in claimed]
    return pending


def _generate_last_context(root: str, conn, chapter: int) -> str:
    fs_rows = db.list_foreshadow(conn)
    overdue = fs_mod.overdue(fs_rows, chapter)
    open_fs = [f for f in fs_rows if f["status"] not in fs_mod.TERMINAL]
    threads = [t for t in db.list_threads(conn) if t["status"] in ("planned", "active", "dormant")]
    prog = load_progress(root)
    outline = parse_outline(paths(root)["outline"])
    vol = volume_of(prog, chapter + 1)
    vrec = __import__("_common").find_volume_record(outline, vol)
    states = db.all_character_states(conn)
    warn = [dict(r) for r in conn.execute(
        "SELECT code,message,chapter FROM warnings WHERE resolved=0 "
        "ORDER BY id DESC LIMIT 12").fetchall()]
    lines = ["# 上一轮收尾上下文（自动生成）", "",
             "> 由 canon.db 派生。事实来源是 Canon，不是本文件。", "",
             "## 当前进度",
             "- 已提交章节：%d" % chapter,
             "- 下一章：ch-%04d（第 %d 卷）" % (chapter + 1, vol),
             "- 当前卷目标：%s" % (vrec.get("goal") or "（未填）"),
             "- 卷末不可逆变化：%s" % (vrec.get("irreversible_change") or "（未填）"), "",
             "## 未回收伏笔（%d）" % len(open_fs)]
    for f in open_fs[:20]:
        lines.append("- %s《%s》状态=%s 计划回收=%s-%s%s"
                     % (f["code"], f["title"], f["status"], f["planned_payoff_start"],
                        f["planned_payoff_end"],
                        "【逾期】" if any(o["code"] == f["code"] for o in overdue) else ""))
    lines += ["", "## 未解决线程（%d）" % len(threads)]
    for t in threads[:20]:
        lines.append("- %s《%s》%s 状态=%s" % (t["code"], t["name"], t["kind"], t["status"]))
    lines += ["", "## 人物当前位置"]
    for s in states[:24]:
        lines.append("- %s：%s｜目标=%s｜状态=%s"
                     % (s["name"], s.get("location") or "-", s.get("goal") or "-",
                        s.get("status") or "-"))
    lines += ["", "## 待人工确认的 warning（%d）" % len(warn)]
    for w in warn:
        lines.append("- [ch-%s][%s] %s" % (w["chapter"], w["code"], w["message"]))
    lines += ["", "## 下一步"]
    lines.append("- 运行 next 获取下一轮简报；优先处理逾期伏笔与未解决线程。")
    return "\n".join(lines) + "\n"


def cmd_round_done(args) -> int:
    root = args.root
    if not db.db_exists(root):
        die("state/canon.db 不存在：运行 migrate")
    conn = db.connect(root)
    tx_mod.recover(root, conn, apply=True)
    prog = load_progress(root)
    st = status_dict(root)
    committed = chapter_numbers(root)
    open_round = db.latest_open_round(conn)
    round_index = int(open_round["round_index"]) if open_round else prog["rounds_completed"] + 1
    planned = list(open_round["planned_chapters"]) if open_round else []
    inferred = False
    if not open_round:
        planned = _infer_legacy_round(conn, committed, prog)
        inferred = True
        db.start_round(conn, round_index, planned)

    blockers, notes = [], []
    missing = [c for c in planned if c not in committed]
    if missing:
        blockers.append("本轮计划 %d 章，实际提交 %d 章；缺少 ch-%s"
                        % (len(planned), len(committed), ", ch-".join("%04d" % c for c in missing)))

    for c in planned:
        if c not in committed:
            continue
        row = db.chapter_row(conn, c)
        a1 = db.audit_for(conn, c, level=1)
        a2 = db.audit_for(conn, c, level=2)
        if row and row["audit_status"] == "imported":
            notes.append("ch-%04d 为导入章节，无原生审计记录" % c)
            continue
        if not (a1 and a1["pass"]) or not (a2 and a2["pass"]):
            blockers.append("ch-%04d 缺少通过的审计（Level1=%s Level2=%s）"
                            % (c, bool(a1 and a1["pass"]), bool(a2 and a2["pass"])))
        gates = db.get_gates(conn, c)
        for g in ("plan", "audit", "canon"):
            if gates.get(g) in ("missing", "fail"):
                blockers.append("ch-%04d 的 %s 门未通过（%s）" % (c, g, gates.get(g)))

    work = tx_mod.pending_work(root, conn)
    for d in work["uncommitted_drafts"]:
        blockers.append("存在未处理的草稿 drafts/ch-%04d.md" % d["chapter"])
    if work["part_files"]:
        blockers.append("parts/ 下仍有 %d 个未合并分节文件" % len(work["part_files"]))
    if work["open_transactions"]:
        blockers.append("存在未完成事务：%s" % [t["id"] for t in work["open_transactions"]])

    db_rows = {r["number"] for r in db.chapters_all(conn)}
    if db_rows != set(committed):
        blockers.append("Canon 章节记录与磁盘章节文件不一致：DB=%d 文件=%d"
                        % (len(db_rows), len(committed)))
    for c in committed:
        if db.load_snapshot(root, c) is None:
            blockers.append("ch-%04d 缺少 snapshot" % c)
    if st["chapter_gaps"]:
        blockers.append("章节号不连续：缺 %s" % st["chapter_gaps"][:8])
    if int(prog.get("chapters_committed", -1)) != len(committed):
        blockers.append("progress.json 记录的 chapters_committed=%s 与磁盘 %d 章不一致"
                        % (prog.get("chapters_committed"), len(committed)))

    import validate_state as vs
    report = vs.validate(root)
    hard = [f for f in report["findings"] if f["severity"] == "error"]
    if hard:
        blockers.append("状态校验发现 %d 个错误：%s"
                        % (len(hard), "、".join(sorted({f["code"] for f in hard}))[:200]))

    waivers = [dict(r) for r in conn.execute(
        "SELECT chapter,code,message FROM warnings WHERE code='GATE_WAIVED' "
        "AND resolved=0").fetchall()]

    if blockers and not args.abandon:
        db.close_round(conn, round_index, "ROUND_BLOCKED", blockers=blockers,
                       waivers=waivers, status="blocked")
        conn.close()
        jprint({"ok": False, "verdict": "ROUND_BLOCKED", "round_index": round_index,
                "round_complete": False, "blockers": blockers,
                "notes": notes, "waivers": waivers,
                "instruction": "修复上述问题后重新运行 round-done；"
                               "如要放弃本轮，使用 --abandon --reason \"...\""})
        return 3

    if blockers and args.abandon:
        if not args.reason:
            conn.close()
            die("--abandon 需要 --reason 说明放弃原因")
        moved = []
        for d in work["uncommitted_drafts"]:
            dst_dir = os.path.join(paths(root)["drafts"], "abandoned")
            os.makedirs(dst_dir, exist_ok=True)
            dst = os.path.join(dst_dir, os.path.basename(d["path"]))
            os.replace(d["path"], dst)
            moved.append(dst)
        db.close_round(conn, round_index, "ROUND_ABANDONED", blockers=blockers,
                       waivers=waivers, status="abandoned", notes=args.reason)
        db.log_change(conn, "round_abandoned", actor=args.actor, entity_kind="round",
                      entity_ref=str(round_index), reason=args.reason)
        conn.close()
        jprint({"ok": True, "verdict": "ROUND_ABANDONED", "round_index": round_index,
                "round_complete": False, "blockers": blockers, "reason": args.reason,
                "drafts_moved": moved,
                "rounds_completed": prog["rounds_completed"]})
        return 0

    # ---- legal round: close it
    if args.context_file:
        content = read_text(args.context_file)
    else:
        content = _generate_last_context(root, conn, max(committed or [0]))
    atomic_write_text(paths(root)["last_context"], content)

    prog["rounds_completed"] = int(prog.get("rounds_completed", 0)) + 1
    prog["last_round_at"] = now_iso()
    save_progress(root, prog)
    verdict = "STOP" if st["complete"] else "REINCARNATE"
    db.close_round(conn, round_index, verdict, blockers=[], waivers=waivers, status="closed",
                   notes=("legacy round inferred" if inferred else ""))
    for c in committed:
        db.round_task(conn, round_index, c, "closed", "done")
    nxt = ("目标达成，导出并停止轮回。" if verdict == "STOP" else
           "上下文即将耗尽：本轮到此为止。下一轮开新上下文，先执行 %s"
           % prog["loop_protocol"]["resume_command"])
    _resync(root, conn)
    conn.close()
    jprint({"ok": True, "verdict": verdict, "round_complete": True,
            "round_index": round_index, "rounds_completed": prog["rounds_completed"],
            "planned": len(planned), "committed_in_round": len(planned) - len(missing),
            "waivers": waivers, "notes": notes,
            "last_context": paths(root)["last_context"], "next": nxt,
            "status": status_dict(root)})
    return 0


# ================================================================ recover / rollback

def cmd_recover(args) -> int:
    root = args.root
    conn = db.connect(root) if db.db_exists(root) else None
    result = tx_mod.recover(root, conn, apply=not args.dry_run)
    if conn is not None and not args.dry_run:
        result["resync"] = _resync(root, conn)
    if conn is not None:
        result["pending"] = tx_mod.pending_work(root, conn)
        conn.close()
    if args.json or True:
        jprint({"ok": True, "dry_run": args.dry_run, **result})
    return 0


def cmd_rollback(args) -> int:
    root = args.root
    if not db.db_exists(root):
        die("state/canon.db 不存在")
    if args.to_chapter is not None:
        keep = int(args.to_chapter)
    elif args.chapter is not None:
        keep = int(args.chapter) - 1
    else:
        die("rollback 需要 --to-chapter N（保留 1..N 章）或 --chapter N（撤销第 N 章）")
    if keep < 0:
        die("rollback 目标章节必须 >= 0")

    conn = db.connect(root)
    committed = chapter_numbers(root)
    # Windows cannot replace a file that another handle holds open: close the
    # connection before the replay swaps canon.db in place.
    conn.close()
    info = db.restore_to_chapter(root, keep)
    conn = db.connect(root)

    moved = []
    quarantine = os.path.join(paths(root)["state"], "rolled-back")
    os.makedirs(quarantine, exist_ok=True)
    # Quarantine every artifact of the undone chapters: chapter files, drafts,
    # plans, deltas and audit reports. Nothing is deleted, and the project is
    # left in exactly the state it had at `keep`.
    for n in committed:
        if n <= keep:
            continue
        for src, label in (
                (chapter_path(root, n), "ch-%04d.txt" % n),
                (staging_chapter_path(root, n), "draft-ch-%04d.md" % n),
                (os.path.join(paths(root)["plans"], "ch-%04d.plan.json" % n),
                 "plan-ch-%04d.json" % n),
                (os.path.join(paths(root)["plans"], "ch-%04d.delta.json" % n),
                 "delta-ch-%04d.json" % n),
                (os.path.join(paths(root)["audits"], "ch-%04d.json" % n),
                 "audit-ch-%04d.json" % n)):
            if os.path.isfile(src):
                os.replace(src, os.path.join(quarantine, label))
                moved.append(os.path.join(quarantine, label))
        row = db.chapter_row(conn, n)
        if row:
            db.log_change(conn, "rollback_chapter", actor=args.actor,
                          entity_kind="chapter", entity_ref="ch-%04d" % n,
                          before={"committed": True}, after={"committed": False},
                          reason=args.reason or "rollback", chapter=n)

    prog = load_progress(root)
    prog["chapters_committed"] = len(chapter_numbers(root))
    prog["highest_chapter"] = max(chapter_numbers(root) or [0])
    prog["last_rollback_at"] = now_iso()
    save_progress(root, prog)
    res = _resync(root, conn)
    db.log_change(conn, "rollback", actor=args.actor, entity_kind="project",
                  entity_ref=str(keep), reason=args.reason or "rollback",
                  after={"to_chapter": keep, "files_moved": len(moved)})
    conn.close()
    chapter_files = [p for p in moved if os.path.basename(p).startswith("ch-")]
    jprint({"ok": True, "to_chapter": keep, "backup_db": info["backup"],
            "snapshots_applied": info["snapshots_applied"],
            "base_used": info["base_used"],
            "snapshot_files_removed": info["snapshot_files_removed"],
            "chapter_files_quarantined": chapter_files,
            "artifacts_quarantined": len(moved), "derived": res,
            "next": 'python scripts/novel_state.py status --root "%s"' % root})
    return 0


# ================================================================ validate

def cmd_validate(args) -> int:
    import validate_state as vs
    report = vs.validate(args.root)
    if args.report:
        atomic_write_text(args.report, vs.render(report))
    if args.json:
        jprint(report)
    else:
        print(vs.render(report))
    return 1 if report["errors"] else 0


# ================================================================ auto-next

def cmd_auto_next(args) -> int:
    """Plan -> write -> audit -> extract -> commit, fully automated.

    Observable (one JSON line per step), interruptible (Ctrl+C closes the round
    cleanly and prints the resume command), resumable (each chapter is its own
    transaction and the round plan is in the database). Off by default.
    """
    import subprocess

    root = args.root
    if not args.writer:
        die("auto-next 需要 --writer <命令>：该命令从 stdin 读取简报 JSON，"
            "把本章正文写到 stdout；不提供写作者就无法自动生成正文。")

    log_path = os.path.join(paths(root)["state"], "auto.log")

    def log(obj):
        line = json.dumps(obj, ensure_ascii=False)
        append_text(log_path, line + "\n")
        print(line, flush=True)

    def run_command(cmd, payload, label):
        # The agent protocol is UTF-8 on stdin/stdout. On Windows the default
        # stdio encoding is the ANSI code page, which would mangle the JSON
        # brief before the agent ever sees it.
        env = dict(os.environ)
        env.setdefault("PYTHONIOENCODING", "utf-8")
        proc = subprocess.run(cmd, shell=True, input=payload.encode("utf-8"),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=args.timeout, env=env)
        if proc.returncode != 0:
            raise NarrativeError("%s 退出码 %d：%s"
                                 % (label, proc.returncode,
                                    proc.stderr.decode("utf-8", "replace")[-1500:]))
        return proc.stdout.decode("utf-8", "replace")

    def run_writer(brief):
        payload = json.dumps(brief, ensure_ascii=False)
        if args.writer_file:
            atomic_write_text(args.writer_file, payload)
            return run_command(args.writer.replace("{brief}", args.writer_file),
                               payload, "writer")
        return run_command(args.writer, payload, "writer")

    def run_extractor(chapter, text):
        """Ask the Extractor agent for a Canon Delta; validate before use."""
        conn = db.connect(root)
        prog = load_progress(root)
        outline = _outline_and_sync(root, conn, prog)
        canon = ctxb.canon_view(conn, prog, outline)
        row = db.get_plan(conn, chapter, "chapter")
        plan = row["payload"] if row else {}
        conn.close()
        prompt = extract_mod.build_prompt(
            title=prog.get("title") or "", chapter=chapter, text=text, canon_view=canon,
            plan=plan, timeline_last=canon.get("timeline_last") or "",
            forbidden=plan.get("forbidden_reveals") or [])
        raw = run_command(args.extractor, prompt, "extractor")
        delta = llm_mod.extract_json_block(raw)
        delta.setdefault("chapter", chapter)
        delta.setdefault("source", "extractor-agent")
        path = extract_mod.delta_path_for(root, chapter)
        atomic_write_json(path, delta)
        return path

    done = 0
    try:
        while done < args.max_chapters:
            brief = _brief_via_args(root, args)
            action = brief.get("action")
            log({"step": "next", "action": action,
                 "round": brief.get("round_index"),
                 "chapters": brief.get("chapters_to_write")})
            if action == "STOP":
                break
            if action == "ROUND_DONE":
                rc = cmd_round_done(argparse.Namespace(root=root, context_file="",
                                                       abandon=False, reason="",
                                                       actor="auto-next"))
                log({"step": "round-done", "exit": rc})
                if rc != 0:
                    break
                continue
            chapter = brief["chapters_to_write"][0]
            # 1. plan
            plan_rc = cmd_plan(argparse.Namespace(
                root=root, chapter=chapter, file="", out="", check=False,
                volume_plan="", chars_per_chapter=0, chapters_per_round=0,
                target=0, estimated_chapters=0))
            log({"step": "plan", "chapter": chapter, "exit": plan_rc})
            if plan_rc != 0:
                break
            # 2. write
            text = canonical_text(run_writer(brief))
            draft = os.path.join(paths(root)["drafts"], "ch-%04d.md" % chapter)
            atomic_write_text(draft, text)
            log({"step": "write", "chapter": chapter, "chars": len(text.strip()),
                 "draft": draft})
            # 3. extract canon (Extractor agent or rules)
            delta_path = ""
            if args.extractor:
                try:
                    delta_path = run_extractor(chapter, text)
                    log({"step": "extract", "chapter": chapter, "delta": delta_path})
                except NarrativeError as exc:
                    log({"step": "extract", "chapter": chapter, "error": str(exc),
                         "fallback": "rule"})
            # 4. audit
            audit_rc = cmd_audit(argparse.Namespace(
                root=root, chapter=chapter, file=draft, delta="", llm=args.llm,
                json=True, strict=True))
            log({"step": "audit", "chapter": chapter, "exit": audit_rc})
            if audit_rc != 0 and not args.continue_on_fail:
                log({"step": "stop", "why": "audit failed; draft kept for revision"})
                break
            # 5. commit
            commit_rc = cmd_commit(argparse.Namespace(
                root=root, chapter=chapter, file=draft, summary="", place="", threads="",
                people="", overwrite=False, allow_short=False, allow_long=False,
                allow_plan=False, allow_audit=False, allow_canon=False,
                delta=delta_path, llm=args.llm, state_source=args.state_source,
                timeout=args.timeout, actor="auto-next", reason="auto-next"))
            log({"step": "commit", "chapter": chapter, "exit": commit_rc})
            if commit_rc != 0:
                break
            done += 1
    except KeyboardInterrupt:
        log({"step": "interrupted", "done": done,
             "resume": 'python scripts/novel_state.py next --root "%s"' % root})
        return 130
    log({"step": "done", "chapters": done, "log": log_path})
    return 0


def _brief_via_args(root: str, args) -> dict:
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cmd_next(argparse.Namespace(root=root, json_output=True, no_recover=False))
    return json.loads(buf.getvalue())


# ================================================================ parser

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    i = sub.add_parser("init", help="create project skeleton + Canon DB")
    i.add_argument("--root", required=True)
    i.add_argument("--title", required=True)
    i.add_argument("--premise", default="")
    i.add_argument("--target", type=int, default=DEFAULT_TARGET)
    i.add_argument("--chars-per-chapter", type=int, default=DEFAULT_CHARS_PER_CHAPTER)
    i.add_argument("--chapters-per-round", type=int, default=DEFAULT_CHAPTERS_PER_ROUND)
    i.add_argument("--volumes", type=int, default=DEFAULT_VOLUMES)
    i.add_argument("--count-mode", choices=COUNT_MODES, default=DEFAULT_COUNT_MODE)
    i.add_argument("--llm-provider", default="rule")
    i.add_argument("--force", action="store_true")
    i.set_defaults(func=cmd_init)

    m = sub.add_parser("migrate", help="upgrade a pre-upgrade project to Canon DB")
    m.add_argument("--root", required=True)
    m.add_argument("--backfill-snapshots", action="store_true", default=True)
    m.add_argument("--no-backfill-snapshots", dest="backfill_snapshots",
                   action="store_false")
    m.set_defaults(func=cmd_migrate)

    s = sub.add_parser("status", help="show progress")
    s.add_argument("--root", required=True)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_status)

    n = sub.add_parser("next", help="emit the round brief (soul-transfer entry point)")
    n.add_argument("--root", required=True)
    n.add_argument("--json", dest="json_output", action="store_true", default=True)
    n.add_argument("--no-recover", action="store_true")
    n.set_defaults(func=cmd_next)

    c = sub.add_parser("commit", help="transactionally commit one audited chapter")
    c.add_argument("--root", required=True)
    c.add_argument("--chapter", type=int, required=True)
    c.add_argument("--file", default="")
    c.add_argument("--summary", default="", help="(deprecated: derived from the canon delta)")
    c.add_argument("--place", default="", help="(deprecated: derived from the canon delta)")
    c.add_argument("--threads", default="", help="(deprecated: derived from the canon delta)")
    c.add_argument("--people", default="", help="(deprecated: derived from the canon delta)")
    c.add_argument("--delta", default="", help="explicit Canon Delta JSON")
    c.add_argument("--llm", default="", help="llm provider spec for canon extraction")
    c.add_argument("--state-source", default="delta", choices=("delta", "plan"))
    c.add_argument("--timeout", type=int, default=300)
    c.add_argument("--actor", default="agent")
    c.add_argument("--reason", default="")
    c.add_argument("--overwrite", action="store_true")
    c.add_argument("--allow-short", action="store_true")
    c.add_argument("--allow-long", action="store_true")
    c.add_argument("--allow-plan", action="store_true")
    c.add_argument("--allow-audit", action="store_true")
    c.add_argument("--allow-canon", action="store_true")
    c.set_defaults(func=cmd_commit)

    r = sub.add_parser("round-done", help="close one round (strict validation)")
    r.add_argument("--root", required=True)
    r.add_argument("--context-file", default="")
    r.add_argument("--abandon", action="store_true", help="explicitly abandon the round")
    r.add_argument("--reason", default="")
    r.add_argument("--actor", default="agent")
    r.set_defaults(func=cmd_round_done)

    im = sub.add_parser("import", help="adopt existing prose as committed chapters")
    im.add_argument("--root", required=True)
    im.add_argument("--group", action="append", required=True,
                    help="glob of chapter files; repeat in chapter order")
    im.add_argument("--start-chapter", type=int, default=1)
    im.add_argument("--limit", type=int, default=0)
    im.add_argument("--summary-prefix", default="导入：")
    im.add_argument("--overwrite", action="store_true")
    im.set_defaults(func=cmd_import)

    pl = sub.add_parser("plan", help="chapter planning (or legacy volume/length planning)")
    pl.add_argument("--root", required=True)
    pl.add_argument("--chapter", type=int, default=None, help="plan this chapter")
    pl.add_argument("--file", default="", help="agent-authored plan JSON to merge onto the skeleton")
    pl.add_argument("--out", default="", help="where to write the plan JSON")
    pl.add_argument("--check", action="store_true", help="validate only")
    pl.add_argument("--volume-plan", default="",
                    help="legacy: comma list of chapters per volume, e.g. 42,50,50,50,50")
    pl.add_argument("--chars-per-chapter", type=int, default=0)
    pl.add_argument("--chapters-per-round", type=int, default=0)
    pl.add_argument("--target", type=int, default=0)
    pl.add_argument("--estimated-chapters", type=int, default=0)
    pl.set_defaults(func=cmd_plan)

    sp = sub.add_parser("scene-plan", help="expand a chapter plan into scene work orders")
    sp.add_argument("--root", required=True)
    sp.add_argument("--chapter", type=int, required=True)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_scene_plan)

    ex = sub.add_parser("extract", help="chapter -> structured Canon Delta (no Canon writes)")
    ex.add_argument("--root", required=True)
    ex.add_argument("--chapter", type=int, required=True)
    ex.add_argument("--file", default="")
    ex.add_argument("--delta", default="", help="pass through an existing delta")
    ex.add_argument("--llm", default="")
    ex.add_argument("--state-source", default="delta", choices=("delta", "plan"))
    ex.add_argument("--timeout", type=int, default=300)
    ex.add_argument("--out", default="")
    ex.add_argument("--json", action="store_true")
    ex.set_defaults(func=cmd_extract)

    au = sub.add_parser("audit", help="Level 1 + Level 2 audit of a draft or chapter")
    au.add_argument("--root", required=True)
    au.add_argument("--chapter", type=int, required=True)
    au.add_argument("--file", default="")
    au.add_argument("--delta", default="")
    au.add_argument("--llm", default="")
    au.add_argument("--json", action="store_true")
    au.add_argument("--strict", action="store_true")
    au.set_defaults(func=cmd_audit)

    pa = sub.add_parser("part", help="append one part to a long chapter draft")
    pa.add_argument("--root", required=True)
    pa.add_argument("--chapter", type=int, required=True)
    pa.add_argument("--file", default="")
    pa.add_argument("--check", action="store_true")
    pa.add_argument("--overwrite", action="store_true")
    pa.set_defaults(func=cmd_part)

    rc = sub.add_parser("recover", help="finish or discard interrupted commits")
    rc.add_argument("--root", required=True)
    rc.add_argument("--dry-run", action="store_true")
    rc.add_argument("--json", action="store_true")
    rc.set_defaults(func=cmd_recover)

    rb = sub.add_parser("rollback", help="roll Canon back to a chapter")
    rb.add_argument("--root", required=True)
    rb.add_argument("--to-chapter", type=int, default=None)
    rb.add_argument("--chapter", type=int, default=None)
    rb.add_argument("--reason", default="")
    rb.add_argument("--actor", default="human")
    rb.set_defaults(func=cmd_rollback)

    va = sub.add_parser("validate", help="full state validation")
    va.add_argument("--root", required=True)
    va.add_argument("--json", action="store_true")
    va.add_argument("--report", default="")
    va.set_defaults(func=cmd_validate)

    an = sub.add_parser("auto-next", help="automated plan->write->audit->extract->commit")
    an.add_argument("--root", required=True)
    an.add_argument("--writer", default="", help="command: brief JSON on stdin, prose on stdout")
    an.add_argument("--writer-file", default="", help="write the brief here and use {brief} in --writer")
    an.add_argument("--extractor", default="",
                    help="Extractor agent command: prompt on stdin, Canon Delta JSON on stdout")
    an.add_argument("--llm", default="")
    an.add_argument("--state-source", default="delta", choices=("delta", "plan"))
    an.add_argument("--max-chapters", type=int, default=1)
    an.add_argument("--timeout", type=int, default=600)
    an.add_argument("--continue-on-fail", action="store_true")
    an.set_defaults(func=cmd_auto_next)

    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except NarrativeError as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
