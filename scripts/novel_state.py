#!/usr/bin/env python3
"""Million-word novel state machine: init / status / next / commit / round-done.

This is the durable memory of the 轮回 (reincarnation) loop. Every round of the
loop reads state through `next`, writes one chapter at a time through `commit`,
and closes with `round-done`. Nothing about progress lives only in the model's
context, so a fresh context can always resume exactly where the last one stopped.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _common import (  # noqa: E402
    COUNT_MODES,
    DEFAULT_CHAPTERS_PER_ROUND,
    DEFAULT_CHARS_PER_CHAPTER,
    DEFAULT_COUNT_MODE,
    DEFAULT_TARGET,
    DEFAULT_VOLUMES,
    append_text,
    chapter_numbers,
    chapter_path,
    chapter_stats,
    count_chars,
    die,
    jprint,
    load_progress,
    now_iso,
    paths,
    read_text,
    save_progress,
    status_dict,
    tail_chars,
    volume_bounds,
    volume_of,
    write_text,
)

BIBLE_TEMPLATE = """# 设定圣经（Bible）

> 这是全书唯一的事实来源。每轮开写前必读，写完后如有新设定必须回写。
> 任何与前文冲突的设定都算 bug，必须改新文，不许改圣经。

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
| R1 |  |  | 是 |

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
| F1 |  |  |  | 未回收 |

## 8. 已用尽的素材（避免重复）
- 已用意象：
- 已用桥段：
- 已用金句：
"""

OUTLINE_TEMPLATE = """# 分卷总纲

> 卷数 {volumes}，预估总章节 {estimated} 章，每章约 {chars} 字。
> 每卷结束时必须有一个不可逆的重大变化。

"""

LEDGER_TEMPLATE = """# 剧情台账（Plot Ledger）

> 每章提交后自动追加一行。这是跨轮次防遗忘的唯一凭据。
> 新轮次开写前必须读最后 8 条。

| 章节 | 字数 | 时间地点 | 本章事件 | 新埋/回收伏笔 | 人物状态变化 |
|---|---|---|---|---|---|
"""

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


# ---------------------------------------------------------------- init

def cmd_init(args) -> int:
    root = os.path.abspath(args.root)
    p = paths(root)
    if os.path.isfile(p["progress"]) and not args.force:
        die("progress.json already exists at %s (use --force to reinitialize)" % root)

    for key in ("chapters", "state", "summaries", "export"):
        os.makedirs(p[key], exist_ok=True)

    est = max(1, int(round(args.target / float(args.chars_per_chapter))))
    prog = {
        "schema": 1,
        "title": args.title,
        "premise": args.premise or "",
        "root": root,
        "target_chars": int(args.target),
        "count_mode": args.count_mode,
        "chars_per_chapter": int(args.chars_per_chapter),
        "chars_per_chapter_min": int(args.chars_per_chapter * 0.85),
        "chars_per_chapter_tolerance": [int(args.chars_per_chapter * 0.8), int(args.chars_per_chapter * 1.25)],
        "chapters_per_round": int(args.chapters_per_round),
        "estimated_chapters": est,
        "volume_count": int(args.volumes),
        "volumes": volume_bounds({"estimated_chapters": est, "volume_count": int(args.volumes)}),
        "rounds_completed": 0,
        "chapters_committed": 0,
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "loop_protocol": {
            "state_files": ["state/progress.json", "state/bible.md", "state/outline.md",
                            "state/plot-ledger.md", "state/last-context.md"],
            "resume_command": 'python scripts/novel_state.py next --root "%s"' % root,
            "stop_condition": "total_chars >= target_chars",
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

    # one outline block per volume
    blocks = []
    for b in prog["volumes"]:
        blocks.append(
            "## 第%d卷（第 %d–%d 章）\n\n"
            "- 卷目标（不可逆变化）：\n"
            "- 主要冲突：\n"
            "- 关键转折：\n"
            "- 卷末钩子：\n" % (b["volume"], b["start_chapter"], b["end_chapter"])
        )
    append_text(p["outline"], "\n".join(blocks) + "\n")

    save_progress(root, prog)
    jprint({"ok": True, "root": root, "estimated_chapters": est, "progress": p["progress"]})
    return 0


# ---------------------------------------------------------------- status

def cmd_status(args) -> int:
    st = status_dict(args.root)
    if args.json:
        jprint(st)
        return 0
    print("《%s》" % st["title"])
    print("  进度 : %s / %s 字（%.2f%%）" % (format(st["total_chars"], ","),
                                             format(st["target_chars"], ","), st["percent"]))
    print("  剩余 : %s 字" % format(st["remaining_chars"], ","))
    print("  章节 : 已提交 %d 章，下一章 ch-%04d" % (st["chapters_committed"], st["next_chapter"]))
    print("  当前 : 第 %d / %d 卷" % (st["current_volume"], st["volume_count"]))
    print("  轮次 : 已完成 %d 轮，预计还需 %d 轮" % (st["rounds_completed"], st["estimated_rounds_left"]))
    print("  状态 : %s" % ("[DONE] 已达标，可停止" if st["complete"] else "[LOOP] 未达标，继续轮回"))
    return 0


# ---------------------------------------------------------------- next

def _bible_digest(p: dict, limit: int = 4000) -> str:
    if not os.path.isfile(p["bible"]):
        return "（无圣经）"
    text = read_text(p["bible"])
    return text[:limit]


def _ledger_tail(p: dict, lines: int = 12) -> str:
    if not os.path.isfile(p["ledger"]):
        return "（无台账）"
    rows = [ln for ln in read_text(p["ledger"]).splitlines() if ln.strip().startswith("|")]
    return "\n".join(rows[-lines:]) if rows else "（台账为空）"


def cmd_next(args) -> int:
    """Emit the round brief: everything a fresh context needs to keep writing."""
    root = args.root
    prog = load_progress(root)
    p = paths(root)
    st = status_dict(root)
    mode = prog.get("count_mode", DEFAULT_COUNT_MODE)

    if st["complete"]:
        brief = {
            "action": "STOP",
            "reason": "target reached",
            "status": st,
            "instruction": "全书已达目标字数，运行 export 后停止轮回，不要再写新章。",
        }
        jprint(brief)
        return 0

    start = st["next_chapter"]
    per_round = int(prog.get("chapters_per_round", DEFAULT_CHAPTERS_PER_ROUND))
    per_chapter = int(prog.get("chars_per_chapter", DEFAULT_CHARS_PER_CHAPTER))
    lo, hi = prog.get("chars_per_chapter_tolerance", [int(per_chapter * 0.8), int(per_chapter * 1.25)])

    # A round must fit inside one context. With 20,000-char chapters a configured
    # 5-chapter round is 100,000 chars — unwritable. Cap the round by a character
    # budget and let the loop run more, shorter rounds instead of failing.
    round_budget = int(prog.get("round_char_budget") or 40000)
    per_round = max(1, min(per_round, round_budget // max(1, per_chapter)))

    # how many chapters this round still needs to reach the target
    need = st["remaining_chars"]
    rounds_needed = max(1, -(-need // max(1, per_round * per_chapter)))
    count = per_round
    if rounds_needed == 1:
        count = max(1, -(-need // per_chapter))
    count = min(count, max(1, -(-need // max(1, lo)))) if need else 0

    numbers = list(range(start, start + count))
    prev = start - 1
    prev_text = read_text(chapter_path(root, prev)) if prev >= 1 and os.path.isfile(chapter_path(root, prev)) else ""

    vol = volume_of(prog, start)
    vol_bounds = next((b for b in volume_bounds(prog) if b["volume"] == vol), None)

    # Long chapters (>=8000 chars) cannot be produced in one model turn, so the
    # brief switches to part-based drafting with an explicit part budget.
    draft_mode = "parts" if per_chapter >= 8000 else "single"
    part_chars = int(prog.get("part_chars") or 3500)
    parts_needed = max(1, -(-per_chapter // max(1, part_chars))) if draft_mode == "parts" else 1

    brief = {
        "action": "WRITE",
        "round_index": st["rounds_completed"] + 1,
        "status": st,
        "volume": vol,
        "volume_range": vol_bounds,
        "chapters_to_write": numbers,
        "chars_per_chapter": per_chapter,
        "chars_tolerance": [lo, hi],
        "count_mode": mode,
        "draft_mode": draft_mode,
        "part_chars": part_chars,
        "parts_per_chapter": parts_needed,
        "char_rule": "每章按 %s 口径统计，落在 %d–%d 字之间；不足重写，超出拆章。" % (mode, lo, hi),
        "bible_digest": _bible_digest(p),
        "ledger_tail": _ledger_tail(p),
        "previous_chapter_tail": tail_chars(prev_text, 1200) if prev_text else "（这是第一章）",
        "last_context": read_text(p["last_context"])[:2000] if os.path.isfile(p["last_context"]) else "",
        "commit_command": 'python scripts/novel_state.py commit --root "%s" --chapter <N> --file <草稿路径> --summary "<本章事件>"' % root,
        "after_round": 'python scripts/novel_state.py round-done --root "%s"' % root,
        "hard_rules": [
            "只写 brief 里列出的章节，一章一个文件，写完立刻 commit，不要攒着一起提交。",
            "每章必须推进主线；禁止无冲突的过场章、禁止总结式收尾。",
            "禁止复述前文摘要；上一章交代过的信息不许再解释一遍。",
            "人物言行必须符合 bible 的欲望/缺陷；新设定必须回写 bible。",
            "命中 banned-phrases.txt 的句子必须重写。",
            "本轮结束时，无论是否写完所有计划章节，都必须执行 round-done 并写 last-context.md。",
            "本章字数不达标就补写场景，不许用排比句注水。",
        ],
    }

    if draft_mode == "parts":
        brief["part_command"] = (
            'python scripts/novel_state.py part --root "%s" --chapter <N> --file <本节草稿路径>' % root)
        brief["part_check"] = (
            'python scripts/novel_state.py part --root "%s" --chapter <N> --check' % root)
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

    jprint(brief)
    return 0


# ---------------------------------------------------------------- commit

def cmd_commit(args) -> int:
    root = args.root
    prog = load_progress(root)
    p = paths(root)
    mode = prog.get("count_mode", DEFAULT_COUNT_MODE)
    lo, hi = prog.get("chars_per_chapter_tolerance", [2000, 4000])

    src = os.path.abspath(args.file)
    if not os.path.isfile(src):
        die("draft file not found: %s" % src)
    text = read_text(src)
    chars = count_chars(text, mode)

    if not args.allow_short and chars < lo:
        die("chapter too short: %d < %d (%s). 补写场景后重交，禁止用空话注水。" % (chars, lo, mode))
    if chars > hi and not args.allow_long:
        die("chapter too long: %d > %d (%s). 拆成两章后重交。" % (chars, hi, mode))

    number = args.chapter
    existing = chapter_numbers(root)
    if number in existing and not args.overwrite:
        die("chapter %d already committed (use --overwrite to replace)" % number)
    if number > (max(existing) + 1 if existing else 1):
        die("cannot skip ahead: chapter %d, expected <= %d" % (number, (max(existing) + 1) if existing else 1))

    dst = chapter_path(root, number)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copyfile(src, dst)

    # ledger row
    summary = (args.summary or "").replace("|", "/").replace("\n", " ").strip() or "（未填）"
    place = (args.place or "").replace("|", "/") or "-"
    threads = (args.threads or "").replace("|", "/") or "-"
    people = (args.people or "").replace("|", "/") or "-"
    append_text(p["ledger"], "| ch-%04d | %d | %s | %s | %s | %s |\n" % (
        number, chars, place, summary, threads, people))

    # per-chapter summary for cheap re-reading later
    write_text(os.path.join(p["summaries"], "ch-%04d.md" % number),
               "# ch-%04d（%d 字）\n\n%s\n" % (number, chars, summary))

    prog["chapters_committed"] = len(chapter_numbers(root))
    save_progress(root, prog)

    st = status_dict(root)
    jprint({
        "ok": True,
        "chapter": number,
        "chars": chars,
        "mode": mode,
        "total_chars": st["total_chars"],
        "remaining_chars": st["remaining_chars"],
        "percent": st["percent"],
        "complete": st["complete"],
    })
    return 0


# ---------------------------------------------------------------- import

def cmd_import(args) -> int:
    """Adopt pre-existing prose as committed chapters.

    Each --group is a glob expanded in sorted order; groups are concatenated in
    the order given, so you control the book's chapter sequence explicitly.
    Import bypasses the length gate on purpose (legacy material rarely matches
    the current target), but every imported chapter is recorded in the ledger and
    is subject to audit_quality.py afterwards.
    """
    root = args.root
    prog = load_progress(root)
    p = paths(root)
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
    if existing and start <= max(existing):
        if not args.overwrite:
            die("chapter %d.. already exist; use --overwrite or --start-chapter" % start)

    os.makedirs(p["chapters"], exist_ok=True)
    imported = []
    for i, src in enumerate(files):
        if args.limit and i >= args.limit:
            break
        n = start + i
        text = read_text(src).strip() + "\n"
        dst = chapter_path(root, n)
        write_text(dst, text)
        chars = count_chars(text, mode)
        title = ""
        for ln in text.splitlines():
            ln = ln.strip()
            if ln:
                title = ln.lstrip("# ").strip()
                break
        summary = (args.summary_prefix + " " + title).strip() if args.summary_prefix else title or "（导入）"
        append_text(p["ledger"], "| ch-%04d | %d | %s | %s | %s | %s |\n" % (
            n, chars, "-", summary.replace("|", "/"), "-", "-"))
        write_text(os.path.join(p["summaries"], "ch-%04d.md" % n),
                   "# ch-%04d（%d 字）\n\n%s\n" % (n, chars, summary))
        imported.append({"chapter": n, "source": os.path.basename(src), "chars": chars})

    prog["chapters_committed"] = len(chapter_numbers(root))
    save_progress(root, prog)
    st = status_dict(root)
    jprint({
        "ok": True,
        "imported": len(imported),
        "first": imported[0]["chapter"] if imported else None,
        "last": imported[-1]["chapter"] if imported else None,
        "imported_chars": sum(r["chars"] for r in imported),
        "total_chars": st["total_chars"],
        "remaining_chars": st["remaining_chars"],
        "next_chapter": st["next_chapter"],
        "complete": st["complete"],
    })
    return 0


# ---------------------------------------------------------------- plan

def cmd_plan(args) -> int:
    """Set explicit volume boundaries and/or per-chapter length targets.

    Use this when adopting an existing manuscript: `--volume-plan 42,50,50,50,50`
    says volume 1 is the 42 chapters already on disk, so the loop resumes inside
    the correct volume instead of a linear guess.
    """
    root = args.root
    prog = load_progress(root)

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
    jprint({"ok": True, "volumes": prog["volumes"],
            "chars_per_chapter": prog.get("chars_per_chapter"),
            "chapters_per_round": prog.get("chapters_per_round"),
            "target_chars": prog.get("target_chars")})
    return 0


# ---------------------------------------------------------------- part

def staging_path(root: str, number: int) -> str:
    return os.path.join(root, "drafts", "ch-%04d.md" % number)


def cmd_part(args) -> int:
    """Append one part (节) to a long chapter being drafted.

    A 20,000-character chapter cannot be produced reliably in a single model
    turn: output limits truncate it and prose quality decays past a few
    thousand characters. So long chapters are drafted as a sequence of parts
    (typically 3,000-5,000 characters each) into one staging file, and only
    committed once the accumulated length clears the lower bound.
    """
    root = args.root
    prog = load_progress(root)
    mode = prog.get("count_mode", DEFAULT_COUNT_MODE)
    number = args.chapter
    lo, hi = prog.get("chars_per_chapter_tolerance", [2000, 4000])

    existing_chapters = chapter_numbers(root)
    if number in existing_chapters and not args.overwrite:
        die("chapter %d already committed; parts cannot be appended to a finished chapter" % number)

    staging = staging_path(root, number)
    existing = read_text(staging) if os.path.isfile(staging) else ""

    if args.check:
        total = count_chars(existing, mode)
        jprint({
            "chapter": number, "staging": staging, "chars": total,
            "target_range": [lo, hi],
            "remaining_to_min": max(0, lo - total),
            "ready_to_commit": total >= lo,
            "over_limit": total > hi,
        })
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
    write_text(staging, merged)

    total = count_chars(merged, mode)
    part_chars = count_chars(piece, mode)
    jprint({
        "ok": True,
        "chapter": number,
        "part_chars": part_chars,
        "chars": total,
        "target_range": [lo, hi],
        "percent_of_chapter": round(total * 100.0 / hi, 1) if hi else 0,
        "remaining_to_min": max(0, lo - total),
        "ready_to_commit": total >= lo and total <= hi,
        "over_limit": total > hi,
        "staging": staging,
        "next": ("commit --chapter %d --file \"%s\"" % (number, staging)) if total >= lo
                else "继续追加下一节（还差 %d 字）" % max(0, lo - total),
    })
    return 0


# ---------------------------------------------------------------- round-done

def cmd_round_done(args) -> int:
    root = args.root
    prog = load_progress(root)
    p = paths(root)
    st = status_dict(root)

    if args.context_file:
        write_text(p["last_context"], read_text(args.context_file))
    elif not os.path.isfile(p["last_context"]):
        write_text(p["last_context"], CONTEXT_TEMPLATE)

    prog["rounds_completed"] = int(prog.get("rounds_completed", 0)) + 1
    prog["last_round_at"] = now_iso()
    save_progress(root, prog)

    st = status_dict(root)
    if st["complete"]:
        verdict = "STOP"
        nxt = "目标达成，导出并停止轮回。"
    else:
        verdict = "REINCARNATE"
        nxt = "上下文即将耗尽：本轮到此为止。下一轮开新上下文，先执行 %s" % prog["loop_protocol"]["resume_command"]
    jprint({"ok": True, "rounds_completed": prog["rounds_completed"], "verdict": verdict,
            "next": nxt, "status": st})
    return 0


# ---------------------------------------------------------------- main

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    i = sub.add_parser("init", help="create project skeleton + state files")
    i.add_argument("--root", required=True)
    i.add_argument("--title", required=True)
    i.add_argument("--premise", default="")
    i.add_argument("--target", type=int, default=DEFAULT_TARGET)
    i.add_argument("--chars-per-chapter", type=int, default=DEFAULT_CHARS_PER_CHAPTER)
    i.add_argument("--chapters-per-round", type=int, default=DEFAULT_CHAPTERS_PER_ROUND)
    i.add_argument("--volumes", type=int, default=DEFAULT_VOLUMES)
    i.add_argument("--count-mode", choices=COUNT_MODES, default=DEFAULT_COUNT_MODE)
    i.add_argument("--force", action="store_true")
    i.set_defaults(func=cmd_init)

    s = sub.add_parser("status", help="show progress")
    s.add_argument("--root", required=True)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_status)

    n = sub.add_parser("next", help="emit the round brief for a fresh context")
    n.add_argument("--root", required=True)
    n.set_defaults(func=cmd_next)

    c = sub.add_parser("commit", help="validate + commit one chapter")
    c.add_argument("--root", required=True)
    c.add_argument("--chapter", type=int, required=True)
    c.add_argument("--file", required=True)
    c.add_argument("--summary", default="")
    c.add_argument("--place", default="")
    c.add_argument("--threads", default="")
    c.add_argument("--people", default="")
    c.add_argument("--overwrite", action="store_true")
    c.add_argument("--allow-short", action="store_true")
    c.add_argument("--allow-long", action="store_true")
    c.set_defaults(func=cmd_commit)

    r = sub.add_parser("round-done", help="close one 轮回 round")
    r.add_argument("--root", required=True)
    r.add_argument("--context-file", default="")
    r.set_defaults(func=cmd_round_done)

    m = sub.add_parser("import", help="adopt existing prose as committed chapters")
    m.add_argument("--root", required=True)
    m.add_argument("--group", action="append", required=True,
                   help="glob of chapter files; repeat in chapter order")
    m.add_argument("--start-chapter", type=int, default=1)
    m.add_argument("--limit", type=int, default=0, help="max files to import (0 = all)")
    m.add_argument("--summary-prefix", default="导入：")
    m.add_argument("--overwrite", action="store_true")
    m.set_defaults(func=cmd_import)

    pl = sub.add_parser("plan", help="set explicit volume boundaries / length targets")
    pl.add_argument("--root", required=True)
    pl.add_argument("--volume-plan", default="",
                    help="comma list of chapters per volume, e.g. 42,50,50,50,50")
    pl.add_argument("--chars-per-chapter", type=int, default=0)
    pl.add_argument("--chapters-per-round", type=int, default=0)
    pl.add_argument("--target", type=int, default=0)
    pl.add_argument("--estimated-chapters", type=int, default=0)
    pl.set_defaults(func=cmd_plan)

    pa = sub.add_parser("part", help="append one part to a long chapter draft")
    pa.add_argument("--root", required=True)
    pa.add_argument("--chapter", type=int, required=True)
    pa.add_argument("--file", default="", help="part draft to append")
    pa.add_argument("--check", action="store_true", help="report progress only")
    pa.add_argument("--overwrite", action="store_true")
    pa.set_defaults(func=cmd_part)

    return ap


def main() -> int:
    args = build_parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
