#!/usr/bin/env python3
"""Chapter Planning: next -> chapter_plan -> scene_plan -> write -> audit.

WHY
---
Without a plan, chapter N is generated from whatever the model happens to
remember, which is how a million-character book drifts. A plan is a *contract*
between the structure layer (volume/arc/threads/foreshadowing in Canon) and the
prose. It says, before a single character is written:

  * what this chapter must accomplish (goal, core conflict)
  * which main and sub threads it must advance
  * who is in it, and what their Canon state is right now
  * which state transitions MUST exist by the end
  * what information may NOT be revealed yet
  * which unrecovered foreshadowing is relevant, and what to do with it
  * what new canonical state the chapter must leave behind

The deterministic half (`skeleton`) is generated from Canon. The narrative half
(goal, conflict, scenes) is authored by the agent or defaults to a rule-based
stub with `source: rule` plus warnings. `validate_plan` gates writing.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import canon_db as db  # noqa: E402
import foreshadow as fs_mod  # noqa: E402
from _common import NarrativeError, jaccard, tokenize, now_iso  # noqa: E402

PLAN_REQUIRED = ("chapter", "chapter_goal", "core_conflict", "characters", "scenes")

STATEFIELDS = ("location", "goal", "emotion", "belief", "knowledge", "health",
               "possession", "status", "relationship")


# ================================================================ skeleton

def skeleton(conn, prog: dict, chapter: int, context: dict, outline: dict = None) -> dict:
    """Deterministic plan skeleton built from Canon (not from the model).

    Everything Canon can answer is answered here; the agent only fills the
    narrative intent. This is why the plan is cheap and why it cannot silently
    forget the volume goal or an overdue thread.
    """
    l2 = context.get("L2_structure") or {}
    l3 = context.get("L3_canon") or {}
    ent = context.get("entities") or {}
    chars = [c for c in (ent.get("characters") or []) if c]
    states = {s["name"]: s for s in (l3.get("character_states") or [])}

    main_threads = [t for t in (l2.get("main_threads") or [])
                    if t.get("status") in ("active", "planned", "dormant")]
    sub_threads = [t for t in (l2.get("sub_threads") or [])
                   if t.get("status") in ("active", "planned", "dormant")]
    relevant = context.get("relevant_foreshadowing") or []

    plan = {
        "schema": 1,
        "chapter": chapter,
        "source": "rule",
        "created_at": now_iso(),
        "volume": l2.get("volume"),
        "arc": (l2.get("arcs") or [{}])[0].get("code") if l2.get("arcs") else None,
        "chapter_goal": "",
        "core_conflict": "",
        "must_advance_main": [t["code"] for t in main_threads[:2]],
        "must_advance_sub": [t["code"] for t in sub_threads[:2]],
        "characters": chars,
        "character_state_at_entry": {
            n: {"location": states.get(n, {}).get("location"),
                "goal": states.get(n, {}).get("goal"),
                "emotion": states.get(n, {}).get("emotion"),
                "status": states.get(n, {}).get("status")}
            for n in chars},
        "location": (ent.get("locations") or [None])[0],
        "timeline": {"world_time": (context.get("L2_structure") or {}).get("timeline_last")
                     or (l3.get("timeline") or [{}])[-1].get("world_time") or ""},
        "must_change": [],
        "allowed_new_info": [],
        "forbidden_reveals": list(context.get("forbidden_reveals") or []),
        "relevant_foreshadowing": [
            {"code": f["code"], "status": f["status"], "why": "；".join(f.get("reasons") or []),
             "planned_payoff_start": f.get("planned_payoff_start"),
             "planned_payoff_end": f.get("planned_payoff_end"),
             "overdue": f.get("overdue")}
            for f in relevant],
        "required_end_state": [],
        "scene_count": 1,
        "scenes": [],
        "questions_to_resolve": [],
        "forbidden_events": list((l2.get("outline_issues") or []) and [] or []),
        "notes": "",
    }
    # required end state: any PAYOFF_READY/overdue thread must be resolved or advanced
    for f in relevant:
        if f.get("overdue") or f.get("status") == "PAYOFF_READY":
            plan["required_end_state"].append(
                "伏笔 %s 必须在本章推进或回收（%s）" % (f["code"], "逾期" if f.get("overdue") else "待回收"))
    if l2.get("volume_irreversible_change"):
        plan["notes"] = "本卷不可逆变化：%s" % l2["volume_irreversible_change"]
    return plan


def default_fill(plan: dict, context: dict) -> tuple:
    """Fill gaps with rule-based, clearly-labelled defaults. Returns (plan, warnings)."""
    warnings = []
    l0 = context.get("L0_current") or {}
    if not plan.get("chapter_goal"):
        plan["chapter_goal"] = "推进%s" % (
            "；".join(plan.get("must_advance_main") or plan.get("must_advance_sub") or ["主线"]) )
        plan["source"] = "rule"
        warnings.append({"code": "PLAN_GOAL_DEFAULTED",
                         "message": "chapter_goal 缺失，已用主线/支线编号生成占位目标"})
    if not plan.get("core_conflict"):
        plan["core_conflict"] = "（未指定冲突，由写作者按本章目标推导）"
        warnings.append({"code": "PLAN_CONFLICT_DEFAULTED",
                         "message": "core_conflict 缺失——无冲突的章节是百万字小说最常见的注水方式"})
    if not plan.get("scenes"):
        target_chars = int(context.get("L2_structure", {}).get("chars_per_chapter") or 0)
        per_scene = 2500
        n = 1
        plan["scenes"] = [{
            "scene_no": i + 1,
            "location": plan.get("location"),
            "participants": list(plan.get("characters") or [])[:4],
            "conflict": plan.get("core_conflict"),
            "summary": "",
            "dialogue_function": "",
        } for i in range(n)]
        plan["scene_count"] = n
        warnings.append({"code": "PLAN_SCENES_DEFAULTED",
                         "message": "未提供场景规划，已生成 1 个占位场景"})
    if not plan.get("characters"):
        plan["characters"] = list(l0.get("characters") or [])[:6]
    plan["scene_count"] = len(plan.get("scenes") or [])
    return plan, warnings


# ================================================================ validation

def validate_plan(plan: dict, context: dict = None, canon: dict = None,
                  chapter: int = None) -> dict:
    """Gate the plan BEFORE prose is generated. Returns {ok, errors, warnings}."""
    errors, warnings = [], []
    context = context or {}
    canon = canon or {}
    if not isinstance(plan, dict):
        return {"ok": False, "errors": ["chapter plan 不是 JSON 对象"],
                "warnings": [], "plan": {}}

    ch = plan.get("chapter")
    if ch is None:
        errors.append("chapter plan 缺少 chapter")
    elif chapter is not None and int(ch) != int(chapter):
        errors.append("chapter plan 的 chapter=%s 与目标章节 %s 不一致" % (ch, chapter))

    for key in PLAN_REQUIRED:
        if key not in plan or plan.get(key) in (None, "", [], {}):
            errors.append("chapter plan 缺少必填字段 %r" % key)

    if not (plan.get("chapter_goal") or "").strip():
        errors.append("chapter_goal 不能为空：没有目标的章节必然注水")
    if not (plan.get("core_conflict") or "").strip():
        errors.append("core_conflict 不能为空：无冲突章节禁止生成")

    if not (plan.get("must_advance_main") or plan.get("must_advance_sub")):
        errors.append("必须声明至少一个要推进的主线或支线线程")

    chars = plan.get("characters") or []
    if not chars:
        errors.append("必须声明本章涉及人物")
    known_chars = set(canon.get("characters") or [])
    for c in chars:
        if known_chars and c not in known_chars:
            warnings.append({"code": "PLAN_UNKNOWN_CHARACTER",
                             "message": "计划中的人物 %s 不在 Canon 中（将在提交时按正文创建）" % c})

    known_threads = set(canon.get("threads") or [])
    for t in (plan.get("must_advance_main") or []) + (plan.get("must_advance_sub") or []):
        if known_threads and t not in known_threads:
            warnings.append({"code": "PLAN_UNKNOWN_THREAD",
                             "message": "计划推进的线程 %s 不在 Canon 中" % t})

    # must_change and required_end_state must be machine-checkable
    if not (plan.get("must_change") or plan.get("required_end_state")):
        warnings.append({"code": "PLAN_NO_STATE_CHANGE",
                         "message": "本章没有声明任何必须发生的状态变化——"
                                    "无变化的章节通常就是过场章（注水的主要来源）"})
    for i, chg in enumerate(plan.get("must_change") or []):
        if not isinstance(chg, dict):
            errors.append("must_change[%d] 必须是对象" % i)
            continue
        if not chg.get("character"):
            errors.append("must_change[%d] 缺少 character" % i)
        if chg.get("field") not in STATEFIELDS:
            errors.append("must_change[%d] 的 field=%r 非法（合法值：%s）"
                          % (i, chg.get("field"), "|".join(STATEFIELDS)))
        if "after" not in chg:
            errors.append("must_change[%d] 缺少 after：无法校验的规划等于没有规划" % i)

    scenes = plan.get("scenes") or []
    if not scenes:
        errors.append("必须至少规划一个场景")
    for i, sc in enumerate(scenes):
        if not isinstance(sc, dict):
            errors.append("scenes[%d] 必须是对象" % i)
            continue
        if not (sc.get("summary") or sc.get("conflict")):
            warnings.append({"code": "SCENE_NO_CONTENT",
                             "message": "scenes[%d] 既没有 summary 也没有 conflict" % i})
        if not sc.get("conflict"):
            errors.append("scenes[%d] 缺少 conflict：不允许无冲突场景" % i)
        if not sc.get("participants"):
            errors.append("scenes[%d] 缺少 participants" % i)
        loc = sc.get("location") or plan.get("location")
        if loc and canon.get("locations") and loc not in set(canon["locations"]):
            warnings.append({"code": "PLAN_UNKNOWN_LOCATION",
                             "message": "场景地点 %s 不在 Canon 中" % loc})

    # forbidden_reveals must reference something checkable
    for r in plan.get("forbidden_reveals") or []:
        if not isinstance(r, str) or not r.strip():
            errors.append("forbidden_reveals 的元素必须是非空字符串（事实编号或关键词）")

    # scene self-repetition inside the same plan
    seen = []
    for sc in scenes:
        toks = tokenize("%s %s" % (sc.get("conflict") or "", sc.get("summary") or ""))
        for old, idx in seen:
            if jaccard(toks, old) > 0.7:
                warnings.append({"code": "PLAN_SCENE_DUPLICATE",
                                 "message": "scenes[%d] 与 scenes[%d] 冲突描述高度相似，可能是同一场戏写两遍"
                                            % (sc.get("scene_no"), idx)})
        seen.append((toks, sc.get("scene_no")))

    return {"ok": not errors, "errors": errors, "warnings": warnings, "plan": plan}


def diff_plan_vs_delta(plan: dict, delta: dict) -> dict:
    """Did the prose actually deliver the plan? Deterministic comparison used by
    the canon gate: planned state changes must appear in the delta."""
    want = {}
    for chg in plan.get("must_change") or []:
        want[(chg.get("character"), chg.get("field"))] = chg.get("after")
    had = {}
    for chg in delta.get("character_changes") or []:
        had[(chg.get("character"), chg.get("field"))] = chg.get("after")
    missing = [{"character": k[0], "field": k[1], "expected": v}
               for k, v in want.items() if k not in had]
    mismatched = [{"character": k[0], "field": k[1], "expected": want[k], "actual": had[k]}
                  for k in want if k in had and had[k] != want[k]]
    planned_threads = set((plan.get("must_advance_main") or []) +
                          (plan.get("must_advance_sub") or []))
    touched_threads = {t.get("code") for t in delta.get("plot_threads") or []}
    untouched = sorted(planned_threads - touched_threads)
    planned_fs = {f.get("code") for f in plan.get("relevant_foreshadowing") or []}
    touched_fs = {f.get("code") for f in delta.get("foreshadowing") or []}
    fs_untouched = sorted(c for c in planned_fs & touched_fs if False) + \
        sorted(planned_fs - touched_fs)
    return {"missing_state_changes": missing, "mismatched_state_changes": mismatched,
            "untouched_threads": untouched, "untouched_foreshadowing": fs_untouched,
            "ok": not missing and not mismatched}


# ================================================================ scene plan

def scene_plan(plan: dict) -> list:
    """Expand a chapter plan into an explicit per-scene work order."""
    out = []
    total = len(plan.get("scenes") or []) or 1
    for i, sc in enumerate(plan.get("scenes") or [], start=1):
        out.append({
            "scene_no": sc.get("scene_no") or i,
            "of": total,
            "location": sc.get("location") or plan.get("location"),
            "participants": sc.get("participants") or plan.get("characters") or [],
            "conflict": sc.get("conflict") or plan.get("core_conflict"),
            "summary": sc.get("summary") or "",
            "dialogue_function": sc.get("dialogue_function") or "",
            "must_not": plan.get("forbidden_events") or [],
            "entry_state": plan.get("character_state_at_entry") or {},
            "exit_state": sc.get("exit_state") or {},
            "goal": sc.get("goal") or plan.get("chapter_goal"),
        })
    return out


def plan_digest(plan: dict) -> str:
    if not plan:
        return "（无章节计划）"
    lines = ["第%s章计划：" % plan.get("chapter"),
             "- 目标：%s" % (plan.get("chapter_goal") or "（未填）"),
             "- 核心冲突：%s" % (plan.get("core_conflict") or "（未填）"),
             "- 涉及人物：%s" % "、".join(plan.get("characters") or []),
             "- 地点：%s" % (plan.get("location") or "（未填）"),
             "- 推进主线：%s｜支线：%s" % ("、".join(plan.get("must_advance_main") or []) or "-",
                                          "、".join(plan.get("must_advance_sub") or []) or "-")]
    if plan.get("must_change"):
        lines.append("- 必须改变的状态：" + "；".join(
            "%s.%s→%s" % (c.get("character"), c.get("field"), c.get("after"))
            for c in plan["must_change"]))
    if plan.get("relevant_foreshadowing"):
        lines.append("- 相关伏笔：" + "；".join(
            "%s(%s%s)" % (f.get("code"), f.get("status"), " 逾期" if f.get("overdue") else "")
            for f in plan["relevant_foreshadowing"]))
    if plan.get("forbidden_reveals"):
        lines.append("- 禁止提前泄露：" + "、".join(plan["forbidden_reveals"]))
    if plan.get("required_end_state"):
        lines.append("- 本章结束必须形成：" + "；".join(plan["required_end_state"]))
    for sc in plan.get("scenes") or []:
        lines.append("  · 第%s场 @%s 冲突=%s" % (sc.get("scene_no"), sc.get("location"),
                                                sc.get("conflict")))
    return "\n".join(lines)


def load_plan_file(path: str) -> dict:
    if not os.path.isfile(path):
        raise NarrativeError("plan file not found: %s" % path)
    from _common import read_text
    try:
        return json.loads(read_text(path))
    except ValueError as exc:
        raise NarrativeError("plan file is not valid JSON: %s" % exc)
