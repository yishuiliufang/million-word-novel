#!/usr/bin/env python3
"""Narrative audit: Level 1 (text hygiene) + Level 2 (semantic consistency).

LEVEL 1 — text hygiene (deterministic, byte-level)
    length window, paragraph shape, banned AI cliches, template-shaped syntax,
    dialogue density, n-gram self repetition, cross-chapter overlap.

    IMPORTANT CHANGE: n-gram repetition is now an ADVISORY, not a verdict. The
    brief is explicit that 8-gram checks may only be a supporting indicator —
    a chapter can be ruined without repeating a single 8-gram, and a clean long
    chapter can repeat one naturally. Extreme repetition (>= 2x threshold) still
    blocks, because that is a machine artefact, not prose.

LEVEL 2 — narrative consistency (semantic, canon-backed)
    character consistency, knowledge boundary, timeline, location continuity,
    item continuity, relationship continuity, causality, foreshadow lifecycle,
    and semantic plot/scene repetition (POSSIBLE_PLOT_REPETITION) which cannot
    be seen by n-grams at all.

Everything deterministic is checked in Python. The optional LLM pass only ever
ADDS advisories; it can never clear a deterministic failure, and its output must
match a strict verdict schema or it is discarded with a warning.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import canon_db as db  # noqa: E402
import context_builder as ctxb  # noqa: E402
import extract as extract_mod  # noqa: E402
import foreshadow as fs_mod  # noqa: E402
from _common import (  # noqa: E402
    NarrativeError,
    canonical_text,
    chapter_path,
    count_chars,
    jprint,
    load_progress,
    paths,
    read_text,
    sha256_text,
    tolerance,
)

# template-shaped prose that reads as machine-written even at scale
STRUCTURAL_SMELLS = [
    (r"不是[^，。！？]{1,12}，不是[^，。！？]{1,12}，是", "三段否定排比"),
    (r"他/她?不知道的是[，,]", "旁白剧透"),
    (r"这一(刻|瞬间)[，,]", "滥用瞬间"),
    (r"仿佛(整个)?世界", "世界级夸张"),
    (r"[，,]。(?!.)", "断句噪音"),
    (r"(!|！){2,}", "叠用感叹号"),
    (r"[。！？][”\"]?[。！？]", "连续句号"),
]

NAME_TAG = re.compile(r'"([^"\n]{1,200})"([\u4e00-\u9fff]{2,4})(?:说|道|问|答|应|喊|叫)')
QUOTE = re.compile(r'[“"]([^”"\n]{1,300})[”"]')
MOVE_VERBS = ("走", "去", "到", "进", "出", "离开", "回", "赶往", "抵达", "下车", "上车",
              "推门", "跨", "穿过", "上楼", "下楼")

SEVERITY_ERROR = "error"
SEVERITY_WARN = "warning"
SEVERITY_INFO = "info"


def _issue(code, message, severity=SEVERITY_WARN, blocking=False, **extra):
    d = {"code": code, "message": message, "severity": severity, "blocking": blocking}
    d.update(extra)
    return d


# ================================================================ level 1

def ngrams(text: str, n: int = 8) -> Counter:
    """Character n-grams over prose with Markdown scaffolding stripped."""
    clean = re.sub(r"^\s*[>#\-*|]+\s*", "", text, flags=re.M)
    clean = re.sub(r"[>\-*|#]", "", clean)
    clean = re.sub(r"\s+", "", clean)
    return Counter(clean[i:i + n] for i in range(len(clean) - n + 1))


def repetition_threshold(chars: int) -> int:
    """Length-aware bar: 3 for a 3k chapter, rising with length."""
    return max(3, 3 + int((chars - 3000) / 6000))


def repeated_phrases(text: str, n: int = 8, min_count: int = 3, chars: int = 0) -> list:
    if chars:
        min_count = max(min_count, repetition_threshold(chars))
    counts = ngrams(text, n)
    return [{"phrase": k, "count": v} for k, v in counts.most_common(20) if v >= min_count]


def level1(text: str, *, mode="no_ws", lo=2000, hi=4000, banned=(), prev_text: str = None,
           advisory_only_repetition: bool = True) -> dict:
    chars = count_chars(text, mode)
    blocking, advisories = [], []
    metrics = {"chars": chars, "mode": mode, "target_range": [lo, hi]}

    if chars < lo:
        blocking.append(_issue("LENGTH_SHORT", "字数不足：%d < %d" % (chars, lo),
                               SEVERITY_ERROR, True, chars=chars, lo=lo))
    if chars > hi:
        blocking.append(_issue("LENGTH_LONG", "字数超标：%d > %d" % (chars, hi),
                               SEVERITY_ERROR, True, chars=chars, hi=hi))

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    metrics["paragraphs"] = len(lines)
    if lines:
        avg = sum(len(ln) for ln in lines) / float(len(lines))
        metrics["avg_paragraph"] = round(avg, 1)
        if avg > 60:
            blocking.append(_issue("PARAGRAPH_TOO_LONG",
                                   "段落过长：平均 %.0f 字/段（>60 读起来是论文不是小说）" % avg,
                                   SEVERITY_ERROR, True))
    if len(lines) < max(8, chars / 200):
        blocking.append(_issue("TOO_FEW_PARAGRAPHS",
                               "段落太少：%d 段，疑似大段堆砌" % len(lines), SEVERITY_WARN, True))

    hits = []
    for phrase in banned or ():
        c = text.count(phrase)
        if c:
            hits.append((phrase, c))
    metrics["banned_hits"] = len(hits)
    if hits:
        blocking.append(_issue("BANNED_PHRASE",
                               "禁用短语：" + "；".join("「%s」×%d" % (p, c) for p, c in hits[:6]),
                               SEVERITY_ERROR, True, detail=hits))

    for pattern, label in STRUCTURAL_SMELLS:
        m = re.search(pattern, text)
        if m:
            blocking.append(_issue("STRUCTURAL_SMELL",
                                   "句式雷同：%s（%s）" % (label, m.group(0)[:24]),
                                   SEVERITY_WARN, True))

    reps = repeated_phrases(text, 8, 3, chars)
    metrics["repetition_threshold"] = repetition_threshold(chars)
    if reps:
        detail = "；".join("%s×%d" % (r["phrase"], r["count"]) for r in reps[:3])
        hard = [r for r in reps if r["count"] >= 2 * repetition_threshold(chars)]
        if hard:
            blocking.append(_issue("NGROV_REPETITION_EXTREME",
                                   "极端自我重复（辅助指标越界）：%s" % detail,
                                   SEVERITY_ERROR, True, detail=reps))
        elif advisory_only_repetition:
            advisories.append(_issue("NGRAM_REPETITION",
                                     "8-gram 自我重复（辅助指标，不判失败）：%s" % detail,
                                     SEVERITY_INFO, False, detail=reps))
        else:
            blocking.append(_issue("NGRAM_REPETITION", "自我重复：%s" % detail,
                                   SEVERITY_WARN, True, detail=reps))

    if prev_text:
        cur = ngrams(text, 8)
        prev = ngrams(prev_text, 8)
        overlap = sum(min(c, prev.get(k, 0)) for k, c in cur.items())
        total = max(1, sum(cur.values()))
        ratio = overlap / float(total)
        metrics["prev_overlap"] = round(ratio, 4)
        if ratio > 0.12:
            advisories.append(_issue("PREV_OVERLAP",
                                     "与上一章 8-gram 重复率 %.1f%%（>12%%）——辅助指标，"
                                     "请人工确认是否在复述前文" % (ratio * 100),
                                     SEVERITY_WARN, False, ratio=round(ratio, 4)))

    dialogue = len(QUOTE.findall(text))
    metrics["dialogue"] = dialogue
    if dialogue == 0:
        blocking.append(_issue("NO_DIALOGUE", "全章无对话，疑似说明文", SEVERITY_ERROR, True))
    elif dialogue < max(3, chars / 900):
        blocking.append(_issue("LOW_DIALOGUE",
                               "对话偏少：%d 处（建议 ≥%d）"
                               % (dialogue, int(max(3, chars / 900))), SEVERITY_WARN, True))

    return {"level": 1, "metrics": metrics, "blocking": blocking, "advisories": advisories,
            "pass": not blocking}


# ================================================================ level 2

def _event_codes(conn) -> set:
    return {r["code"] for r in conn.execute(
        "SELECT code FROM events WHERE code IS NOT NULL").fetchall()}


def level2(text: str, *, chapter: int, conn, delta: dict = None, plan: dict = None,
           canon: dict = None, timeline_last: str = "") -> dict:
    """Semantic consistency. Every check is explainable and citeable."""
    blocking, advisories = [], []
    delta = delta or {}
    plan = plan or {}
    canon = canon or {}
    metrics = {}

    # ---- 1. causality: every cause must exist
    if conn is not None:
        codes = _event_codes(conn)
        codes |= {e.get("code") for e in delta.get("events") or [] if e.get("code")}
        for ev in delta.get("events") or []:
            for cause in ev.get("causes") or []:
                if cause not in codes:
                    blocking.append(_issue(
                        "DANGLING_CAUSE",
                        "事件 %s 引用了不存在的原因事件 %s（因果链断裂）"
                        % (ev.get("code") or ev.get("summary", "")[:12], cause),
                        SEVERITY_ERROR, True))

    # ---- 2. timeline monotonicity
    tl = (delta.get("timeline") or {}).get("world_time")
    metrics["timeline"] = tl
    if conn is not None:
        rows = db.timeline_all(conn)
        prev_time = rows[-1]["world_time"] if rows else timeline_last
        metrics["timeline_prev"] = prev_time
        if tl and prev_time and str(tl) < str(prev_time):
            blocking.append(_issue("TIMELINE_REGRESSION",
                                   "时间线倒退：本章 %s 早于上一章 %s" % (tl, prev_time),
                                   SEVERITY_ERROR, True))

    # ---- 3. character consistency (status / unknown names)
    if conn is not None:
        dead = {}
        for r in conn.execute("SELECT e.name AS name, s.status AS status FROM character_states s "
                              "JOIN entities e ON e.id=s.entity_id").fetchall():
            if r["status"] in ("dead", "destroyed"):
                dead[r["name"]] = r["status"]
        for ev in delta.get("events") or []:
            for p in ev.get("participants") or []:
                if p in dead:
                    blocking.append(_issue(
                        "DEAD_CHARACTER_ACTIVE",
                        "人物 %s 在 Canon 中状态为 %s，却作为事件参与者出现" % (p, dead[p]),
                        SEVERITY_ERROR, True))
        known = set(canon.get("characters") or [])
        for m in NAME_TAG.finditer(text):
            name = m.group(2)
            if known and name not in known and name not in ("他们", "她们", "我们", "你们"):
                advisories.append(_issue(
                    "UNKNOWN_SPEAKER",
                    "对白标签中的说话人 %s 不在 Canon 人物表中（可能是新人或误判）" % name,
                    SEVERITY_WARN, False))

    # ---- 4. knowledge boundary / forbidden reveals
    forbidden = list(plan.get("forbidden_reveals") or []) + \
        list((plan.get("forbidden_events") or []))
    for item in forbidden:
        if isinstance(item, str) and item and item in text:
            blocking.append(_issue("FORBIDDEN_REVEAL",
                                   "计划禁止提前泄露的 %r 出现在正文中（违反知识边界）" % item,
                                   SEVERITY_ERROR, True))
    if conn is not None:
        secret_facts = [r for r in conn.execute(
            "SELECT DISTINCT fact_code, fact_text FROM knowledge WHERE secret=1").fetchall()]
        for row in secret_facts:
            code, fact_text = row["fact_code"], row["fact_text"] or ""
            if code in text or (fact_text and len(fact_text) >= 4 and fact_text in text):
                knowers = db.who_knows(conn, code)
                if not knowers:
                    advisories.append(_issue(
                        "KNOWLEDGE_BOUNDARY",
                        "机密事实 %s 出现在正文中，但 Canon 中没有任何人知道它" % code,
                        SEVERITY_WARN, False))
    if conn is not None:
        for hit in fs_mod.detect_leaks(db.list_foreshadow(conn), text, chapter):
            advisories.append(_issue("FORESHADOW_LEAK", hit["message"],
                                     SEVERITY_WARN, False, code_detail=hit))

    # ---- 5. location continuity
    if conn is not None and delta:
        states = {s["name"]: s for s in db.all_character_states(conn)}
        for chg in delta.get("character_changes") or []:
            if chg.get("field") != "location":
                continue
            name = chg.get("character")
            before = chg.get("before") if chg.get("before") is not None else \
                (states.get(name) or {}).get("location")
            after = chg.get("after")
            if before and after and before != after:
                moved = any(v in text for v in MOVE_VERBS)
                if not moved:
                    advisories.append(_issue(
                        "LOCATION_CHANGE_UNSTAGED",
                        "人物 %s 的位置从 %s 变为 %s，但正文里找不到任何位移动作"
                        % (name, before, after), SEVERITY_WARN, False))

    # ---- 6. item continuity
    if conn is not None and delta:
        items = {i["code"]: i for i in db.list_items(conn)}
        for it in delta.get("items") or []:
            prev = items.get(it.get("code"))
            if prev is None:
                continue
            old_holder = prev.get("holder_name")
            new_holder = it.get("holder")
            if new_holder and old_holder and new_holder != old_holder:
                if (it.get("code") or "") not in text and (it.get("name") or "␀") not in text:
                    advisories.append(_issue(
                        "ITEM_TRANSFER_UNEXPLAINED",
                        "物品 %s 的持有者从 %s 变为 %s，但正文没有提到该物品"
                        % (it.get("code"), old_holder, new_holder), SEVERITY_WARN, False))

    # ---- 7. relationship continuity
    if conn is not None and delta:
        for rel in delta.get("relationships") or []:
            if rel.get("status") in ("ended", "broken", "betrayed"):
                token = rel.get("kind") or ""
                if token and token not in text:
                    advisories.append(_issue(
                        "RELATIONSHIP_CHANGE_UNSTAGED",
                        "关系 %s→%s 变为 %s，正文中没有直接刻画"
                        % (rel.get("from"), rel.get("to"), rel.get("status")),
                        SEVERITY_WARN, False))

    # ---- 8. foreshadow lifecycle legality
    if conn is not None:
        for f in delta.get("foreshadowing") or []:
            code = f.get("code")
            if not code:
                continue
            row = db.get_foreshadow(conn, code)
            before = row["status"] if row else "PLANTED"
            action = f.get("action") or "touch"
            implied, _, _ = fs_mod.transition_plan(before, action)
            # A declared status is what actually gets written, so validate THAT
            # transition — an extractor may jump RESOLVED -> ACTIVE illegaly.
            target = f.get("status") or implied
            ok, reason = fs_mod.validate_transition(before, target)
            if not ok:
                blocking.append(_issue("ILLEGAL_FORESHADOW_TRANSITION",
                                       "伏笔 %s：%s" % (code, reason), SEVERITY_ERROR, True))
            elif f.get("status") and f["status"] != implied:
                advisories.append(_issue(
                    "FORESHADOW_STATUS_OVERRIDE",
                    "伏笔 %s 声明的状态 %s 与动作 %s 推出的 %s 不一致，以声明为准并记录"
                    % (code, f["status"], action, implied), SEVERITY_WARN, False))

    # ---- 9. semantic plot repetition (n-grams can never see this)
    if delta.get("scenes") and conn is not None:
        for hit in ctxb.scene_similarity_to_history(conn, chapter, delta["scenes"]):
            advisories.append(_issue(hit["code"], hit["message"], SEVERITY_WARN, False,
                                     similarity=hit.get("similarity")))
    if delta.get("events") and conn is not None:
        prior = []
        for n in range(max(1, chapter - 40), chapter):
            prior.extend(db.events_of_chapter(conn, n))
        cur_tokens = ctxb.tokenize(" ".join(
            "%s %s" % (e.get("kind"), e.get("summary")) for e in delta["events"]))
        for old in prior:
            sim = ctxb.jaccard(cur_tokens, ctxb.tokenize("%s %s" % (old["kind"], old["summary"])))
            if sim >= 0.75:
                advisories.append(_issue(
                    "POSSIBLE_PLOT_REPETITION",
                    "本章事件与第%d章《%s》高度相似（%.2f）——文字不同但剧情可能是同一件事"
                    % (old["chapter"], old["summary"][:24], sim), SEVERITY_WARN, False))

    # ---- 10. plan vs delivery
    if plan:
        import chapter_plan as cp
        diff = cp.diff_plan_vs_delta(plan, delta)
        metrics["plan_diff"] = diff
        for m in diff["missing_state_changes"]:
            blocking.append(_issue("PLANNED_STATE_CHANGE_MISSING",
                                   "计划要求 %s.%s 变为 %r，但 Canon Delta 中不存在"
                                   % (m["character"], m["field"], m["expected"]),
                                   SEVERITY_ERROR, True))
        for m in diff["mismatched_state_changes"]:
            blocking.append(_issue("PLANNED_STATE_CHANGE_DIVERGED",
                                   "计划要求 %s.%s=%r，实际 %r"
                                   % (m["character"], m["field"], m["expected"], m["actual"]),
                                   SEVERITY_ERROR, True))
        for t in diff["untouched_threads"]:
            advisories.append(_issue("PLANNED_THREAD_UNTOUCHED",
                                     "计划要推进线程 %s，但 Delta 中没有该线程的变化" % t,
                                     SEVERITY_WARN, False))

    return {"level": 2, "metrics": metrics, "blocking": blocking, "advisories": advisories,
            "pass": not blocking}


# ================================================================ optional LLM pass

VERDICT_SCHEMA = {
    "type": "object",
    "required": ["verdicts"],
    "properties": {"verdicts": {"type": "array", "items": {
        "type": "object", "required": ["code", "severity", "message", "evidence"],
        "properties": {"code": {"type": "string"},
                       "severity": {"enum": ["error", "warning", "info"]},
                       "message": {"type": "string"},
                       "evidence": {"type": "string"}}}}},
}

SEMANTIC_PROMPT = """你是长篇小说的叙事一致性审计器（Consistency Auditor）。
只做判定，不改稿。输出 JSON，不要解释、不要代码围栏。

已知 Canon 事实：
{canon_digest}

计划（本章应完成的事）：
{plan_digest}

第 {chapter} 章正文：
---
{text}
---

请检查：人物一致性、人物知识边界、时间线、地点一致性、物品连续性、关系连续性、因果一致性。
只报告你能在正文中**原文引用**作为证据的问题。证据不足就不要报告。
输出：
{{"verdicts": [{{"code": "KNOWLEDGE_BOUNDARY|TIMELINE|LOCATION|ITEM|RELATION|CAUSALITY|CHARACTER",
               "severity": "error|warning|info", "message": "...", "evidence": "正文原句"}}]}}
"""


def _validate_verdicts(obj) -> tuple:
    from extract import _validate
    errors = []
    _validate(obj, VERDICT_SCHEMA, "verdicts", errors, [])
    return (not errors), errors


def llm_pass(spec: str, *, chapter: int, text: str, canon_digest: str,
             plan_digest: str, timeout: int = 300) -> dict:
    import llm as llm_mod
    prompt = SEMANTIC_PROMPT.format(chapter=chapter, text=text,
                                    canon_digest=canon_digest or "（无）",
                                    plan_digest=plan_digest or "（无）")
    try:
        obj = llm_mod.run_json(spec, prompt, timeout=timeout)
    except NarrativeError as exc:
        return {"ok": False, "verdicts": [], "error": str(exc)}
    ok, errors = _validate_verdicts(obj)
    if not ok:
        return {"ok": False, "verdicts": [], "error": "模型输出不符合判定 schema：%s" % errors[:3]}
    out = []
    for v in obj.get("verdicts") or []:
        out.append(_issue("LLM_%s" % v["code"], v["message"],
                          v["severity"], v["severity"] == "error",
                          evidence=v.get("evidence")))
    return {"ok": True, "verdicts": out, "error": None}


# ================================================================ orchestration

def load_banned(root: str) -> list:
    p = paths(root)["banned"]
    if not os.path.isfile(p):
        return []
    out = []
    for line in read_text(p).splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            out.append(s)
    return out


def audit_chapter(root: str, chapter: int, *, text: str = None, file: str = None,
                  delta: dict = None, plan: dict = None, levels=(1, 2),
                  llm_spec: str = "", conn=None,
                  llm_canon_digest: str = "", llm_plan_digest: str = "") -> dict:
    """Audit one chapter/draft. Returns the full report and (if a DB is open)
    records it in the `audits` table with the exact text hash it applies to.

    The hash is what makes `audit -> commit` enforceable: commit recomputes the
    draft hash and refuses to proceed if no passing audit exists for it.
    """
    prog = load_progress(root)
    mode = prog.get("count_mode", "no_ws")
    lo, hi = tolerance(prog)
    banned = load_banned(root)

    if text is None:
        path = file or chapter_path(root, chapter)
        if not os.path.isfile(path):
            return {"chapter": chapter, "exists": False, "pass": False,
                    "blocking": [_issue("CHAPTER_MISSING", "章节文件缺失：%s" % path,
                                        SEVERITY_ERROR, True)],
                    "advisories": [], "sha256": None}
        text = read_text(path)
        file = os.path.abspath(path)

    # The digest must be computed on exactly the same canonical form commit uses,
    # otherwise a legitimately audited draft could be rejected as "unaudited".
    text = canonical_text(text)
    sha = sha256_text(text)
    prev_text = None
    prev_path = chapter_path(root, chapter - 1)
    if chapter > 1 and os.path.isfile(prev_path):
        prev_text = read_text(prev_path)

    reports = {}
    if 1 in levels:
        reports["level1"] = level1(text, mode=mode, lo=lo, hi=hi, banned=banned,
                                   prev_text=prev_text)
    canon = {}
    outline = None
    own_conn = False
    if 2 in levels:
        if conn is None and db.db_exists(root):
            conn = db.connect(root)
            own_conn = True
        outline = __import__("_common").parse_outline(paths(root)["outline"])
        if conn is not None:
            canon = ctxb.canon_view(conn, prog, outline)
        reports["level2"] = level2(text, chapter=chapter, conn=conn, delta=delta,
                                   plan=plan, canon=canon,
                                   timeline_last=canon.get("timeline_last") or "")

    blocking = []
    advisories = []
    for rep in reports.values():
        blocking.extend(rep["blocking"])
        advisories.extend(rep["advisories"])

    if llm_spec and llm_spec not in ("none", "rule"):
        # The LLM pass only ever ADDS advisories; a deterministic failure cannot be
        # cleared by a model. Its output must match VERDICT_SCHEMA or it is dropped.
        llm_res = llm_pass(llm_spec, chapter=chapter, text=text,
                           canon_digest=llm_canon_digest, plan_digest=llm_plan_digest)
        if not llm_res["ok"]:
            advisories.append(_issue("LLM_AUDIT_UNAVAILABLE",
                                     "LLM 语义审计不可用：%s（已跳过，不影响确定性检查）" % llm_res["error"],
                                     SEVERITY_INFO, False))
        else:
            for v in llm_res["verdicts"]:
                if v["blocking"]:
                    blocking.append(v)
                else:
                    advisories.append(v)

    result = {
        "chapter": chapter,
        "exists": True,
        "file": file,
        "sha256": sha,
        "chars": count_chars(text, mode),
        "count_mode": mode,
        "levels": list(levels),
        "level1": reports.get("level1"),
        "level2": reports.get("level2"),
        "blocking": blocking,
        "advisories": advisories,
        "pass": not blocking,
    }

    if conn is not None:
        for lvl in (1, 2):
            if lvl not in levels:
                continue
            lvl_blocking = (reports.get("level%d" % lvl) or {}).get("blocking", [])
            lvl_adv = (reports.get("level%d" % lvl) or {}).get("advisories", [])
            db.record_audit(conn, chapter, lvl, file=file, sha256=sha,
                            passed=not lvl_blocking,
                            blocking=[{"code": b["code"], "message": b["message"]}
                                      for b in lvl_blocking],
                            advisories=[{"code": a["code"], "message": a["message"]}
                                        for a in lvl_adv],
                            provider="rule")
        db.set_gates(conn, chapter, {"audit": "pass" if result["pass"] else "fail"})
        if own_conn:
            conn.close()
    return result


def render_report(result: dict) -> str:
    lines = []
    if not result.get("exists"):
        lines.append("ch-%04d  缺失" % result["chapter"])
        return "\n".join(lines)
    flag = "OK  " if result["pass"] else "FAIL"
    lines.append("[%s] ch-%04d  %d 字  阻断 %d  提示 %d"
                 % (flag, result["chapter"], result.get("chars") or 0,
                    len(result["blocking"]), len(result["advisories"])))
    for b in result["blocking"]:
        lines.append("     [阻断] %s: %s" % (b["code"], b["message"]))
    for a in result["advisories"]:
        lines.append("     [提示] %s: %s" % (a["code"], a["message"]))
    return "\n".join(lines)


# ================================================================ CLI (audit_quality.py compat)

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Narrative audit (Level 1 text hygiene + "
                                             "Level 2 semantic consistency)")
    ap.add_argument("--root", required=True)
    ap.add_argument("--chapter", type=int, default=None)
    ap.add_argument("--file", default="", help="audit a draft instead of the committed chapter")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--strict", action="store_true", help="exit 1 on any blocking issue")
    ap.add_argument("--level", default="both", choices=("1", "2", "both"))
    ap.add_argument("--delta", default="", help="canon delta to audit against (Level 2)")
    ap.add_argument("--llm", default="", help="llm provider spec for the semantic pass")
    args = ap.parse_args(argv)

    prog = load_progress(args.root)
    nums = __import__("_common").chapter_numbers(args.root)
    levels = {"1": (1,), "2": (2,), "both": (1, 2)}[args.level]

    if args.chapter:
        targets = [args.chapter]
    elif args.all or not nums:
        targets = nums
    else:
        targets = nums[-1:]

    conn = db.connect(args.root) if db.db_exists(args.root) else None
    results = []
    delta = None
    if args.delta:
        delta = extract_mod.load_delta(args.delta)
    for n in targets:
        plan = None
        if conn is not None:
            row = db.get_plan(conn, n, "chapter")
            plan = row["payload"] if row else None
        results.append(audit_chapter(args.root, n, file=args.file or None, delta=delta,
                                     plan=plan, levels=levels, llm_spec=args.llm,
                                     conn=conn))
    if conn is not None:
        conn.close()

    failed = [r for r in results if not r["pass"]]
    out = {"audited": len(results), "passed": len(results) - len(failed),
           "failed": len(failed), "all_pass": not failed, "reports": results}
    if args.json:
        jprint(out)
    else:
        for r in results:
            print(render_report(r))
        print("\n合计：%d 审计，%d 通过，%d 阻断" % (out["audited"], out["passed"], out["failed"]))
        n_advisory = sum(len(r["advisories"]) for r in results)
        if n_advisory:
            print("提示项 %d 条（辅助指标，不判失败；请在正文中人工确认）" % n_advisory)
    if args.strict and failed:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
