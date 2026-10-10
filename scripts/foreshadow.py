#!/usr/bin/env python3
"""Foreshadowing lifecycle management (deterministic).

A million-character novel dies from unrecovered setup: the writer plants 12
threads in volume 1 and forgets 9 of them. Markdown tables cannot prevent that,
because nothing ever *queries* them. This module is the query layer.

LIFECYCLE
---------
    PLANTED -> ACTIVE -> DEVELOPING -> PAYOFF_READY -> RESOLVED
                                                  \\-> STALE_LEAK

Transitions are validated, not assumed. Illegal transitions are reported and
(with `strict=True`) refused; the caller records a warning rather than silently
mutating Canon.

WINDOWS
-------
Every foreshadowing carries `planned_payoff_start`/`planned_payoff_end`. The
state is a function of chapter position inside that window, so "which threads
should I be paying off right now" is a pure computation, not a judgement call.
"""
from __future__ import annotations

FORESHADOW_STATES = ("PLANTED", "ACTIVE", "DEVELOPING", "PAYOFF_READY",
                     "RESOLVED", "STALE_LEAK")

TERMINAL = ("RESOLVED",)

# before -> allowed after
LEGAL_TRANSITIONS = {
    "PLANTED": ("ACTIVE", "DEVELOPING", "STALE_LEAK", "RESOLVED"),
    "ACTIVE": ("DEVELOPING", "PAYOFF_READY", "RESOLVED", "STALE_LEAK"),
    "DEVELOPING": ("ACTIVE", "PAYOFF_READY", "RESOLVED", "STALE_LEAK"),
    "PAYOFF_READY": ("RESOLVED", "DEVELOPING", "STALE_LEAK"),
    "RESOLVED": (),
    "STALE_LEAK": ("RESOLVED", "DEVELOPING"),
}

# how close to the planned payoff window counts as "relevant now"
RELEVANCE_WINDOW = 6
# a PAYOFF_READY item not resolved after this many chapters is overdue
OVERDUE_GRACE = 3


def validate_transition(before, after) -> tuple:
    """Return (ok, reason)."""
    if before is None:
        return True, ""
    if before == after:
        return True, ""
    if before not in LEGAL_TRANSITIONS:
        return False, "非法伏笔状态：%s" % before
    if after not in FORESHADOW_STATES:
        return False, "非法伏笔状态：%s" % after
    if after not in LEGAL_TRANSITIONS[before]:
        return False, "非法状态迁移 %s -> %s" % (before, after)
    return True, ""


def advance(record: dict, chapter: int, action: str = None) -> dict:
    """Compute the lifecycle status implied by an action at a given chapter.

    `action` is what the extractor saw in the prose: plant | touch | develop |
    ready | resolve | leak. Unknown actions leave the status alone.
    """
    cur = record.get("status") or "PLANTED"
    if action == "plant":
        return {"status": "PLANTED", "planted_chapter": record.get("planted_chapter") or chapter}
    if action in ("touch", "advance"):
        nxt = "ACTIVE" if cur == "PLANTED" else cur
        return {"status": nxt, "last_touched_chapter": chapter}
    if action == "develop":
        nxt = {"PLANTED": "DEVELOPING", "ACTIVE": "DEVELOPING",
               "DEVELOPING": "DEVELOPING", "PAYOFF_READY": "PAYOFF_READY",
               "RESOLVED": "RESOLVED", "STALE_LEAK": "DEVELOPING"}.get(cur, "DEVELOPING")
        return {"status": nxt, "last_touched_chapter": chapter}
    if action == "ready":
        if cur == "RESOLVED":
            return {"status": "RESOLVED"}
        return {"status": "PAYOFF_READY", "last_touched_chapter": chapter}
    if action == "resolve":
        return {"status": "RESOLVED", "resolved_chapter": chapter,
                "last_touched_chapter": chapter}
    if action == "leak":
        if cur == "RESOLVED":
            return {"status": "RESOLVED"}
        return {"status": "STALE_LEAK", "leak_detected": True,
                "last_touched_chapter": chapter}
    return {}


def window_phase(record: dict, chapter: int) -> str:
    """Where `chapter` sits relative to the planned payoff window."""
    start = record.get("planned_payoff_start")
    end = record.get("planned_payoff_end") or start
    if start is None:
        return "unknown"
    if chapter < int(start):
        return "before"
    if end is not None and chapter > int(end):
        return "past"
    return "inside"


def lifecycle_status(record: dict, chapter: int) -> dict:
    """Full derived status: legal state + window position + overdue flag."""
    status = record.get("status") or "PLANTED"
    phase = window_phase(record, chapter)
    start = record.get("planned_payoff_start")
    end = record.get("planned_payoff_end") or start
    overdue = False
    if status not in TERMINAL and start is not None:
        if end is not None and chapter > int(end) + OVERDUE_GRACE:
            overdue = True
    if status == "PAYOFF_READY" and record.get("resolved_chapter") is None:
        last = record.get("last_touched_chapter")
        if last is not None and chapter > int(last) + OVERDUE_GRACE:
            overdue = True
    return {
        "code": record.get("code"),
        "title": record.get("title") or "",
        "status": status,
        "kind": record.get("kind") or "long",
        "planted_chapter": record.get("planted_chapter"),
        "planned_payoff_start": start,
        "planned_payoff_end": end,
        "phase": phase,
        "overdue": overdue,
        "leak_detected": bool(record.get("leak_detected")),
        "open": status not in TERMINAL,
    }


def all_statuses(records, chapter: int) -> list:
    return [lifecycle_status(r, chapter) for r in records]


def overdue(records, chapter: int) -> list:
    return [s for s in all_statuses(records, chapter) if s["overdue"]]


def leaking(records) -> list:
    return [lifecycle_status(r, r.get("last_touched_chapter") or 0) for r in records
            if r.get("status") == "STALE_LEAK" or r.get("leak_detected")]


def relevance(record: dict, chapter: int, *, entities=(), thread_codes=(),
              text: str = None) -> tuple:
    """Score how relevant an open foreshadowing is to the chapter being planned.

    Deterministic and explainable: every point comes with a named reason, so the
    brief can tell the writer WHY this thread is being surfaced instead of dumping
    the whole registry into the prompt.
    """
    info = lifecycle_status(record, chapter)
    if not info["open"]:
        return 0.0, []
    score = 0.0
    reasons = []
    phase = info["phase"]
    if phase == "inside":
        score += 5.0
        reasons.append("已进入计划回收窗口(%s-%s)" % (info["planned_payoff_start"],
                                              info["planned_payoff_end"]))
    elif phase == "past":
        score += 7.0
        reasons.append("已超过计划回收窗口，逾期未收")
    elif phase == "before" and info["planned_payoff_start"] is not None:
        dist = int(info["planned_payoff_start"]) - chapter
        if dist <= RELEVANCE_WINDOW:
            score += 3.0
            reasons.append("距计划回收窗口还有 %d 章" % dist)
    if info["status"] == "PAYOFF_READY":
        score += 4.0
        reasons.append("状态为 PAYOFF_READY，应尽快回收")
    if info["status"] == "DEVELOPING":
        score += 1.5
        reasons.append("状态为 DEVELOPING，需要继续推进")
    if info["overdue"]:
        score += 2.0
        reasons.append("逾期标记")
    if info["leak_detected"] or info["status"] == "STALE_LEAK":
        score += 3.0
        reasons.append("疑似提前泄露，需要确认")

    ent = set(entities or ())
    tcodes = set(thread_codes or ())
    data = record.get("data") or {}
    bound_entities = set(data.get("entities") or [])
    if ent & bound_entities:
        score += 4.0
        reasons.append("本章涉及人物 %s" % "、".join(sorted(ent & bound_entities)))
    bound_threads = set(data.get("threads") or [])
    if tcodes & bound_threads:
        score += 3.0
        reasons.append("本章推进线程 %s" % "、".join(sorted(tcodes & bound_threads)))
    if text:
        for token in (data.get("keywords") or []):
            if token and token in text:
                score += 1.0
                reasons.append("关键词命中：%s" % token)
    return score, reasons


def select_relevant(records, chapter: int, *, entities=(), thread_codes=(),
                    text: str = None, limit: int = 12) -> list:
    """The `relevant_foreshadowing` array the writer is actually shown."""
    scored = []
    for r in records:
        score, reasons = relevance(r, chapter, entities=entities,
                                   thread_codes=thread_codes, text=text)
        if score <= 0:
            continue
        info = lifecycle_status(r, chapter)
        info["score"] = round(score, 2)
        info["reasons"] = reasons
        scored.append(info)
    scored.sort(key=lambda x: (-x["score"], str(x["code"])))
    return scored[:limit]


def detect_leaks(records, text: str, chapter: int) -> list:
    """Deterministic leak detection.

    A thread whose keywords appear in prose BEFORE its planned reveal point is a
    leak only if the thread is still meant to be hidden. The conservative rule:
    report it, do not modify Canon.
    """
    hits = []
    for r in records:
        info = lifecycle_status(r, chapter)
        if not info["open"]:
            continue
        data = r.get("data") or {}
        keywords = data.get("secret_keywords") or data.get("keywords") or []
        if not keywords:
            continue
        start = info["planned_payoff_start"]
        if start is not None and chapter >= int(start):
            continue  # already inside the reveal window: not a leak
        found = [k for k in keywords if k and k in (text or "")]
        if found:
            hits.append({"code": info["code"], "title": info["title"],
                         "keywords": found,
                         "message": "伏笔 %s 的关键信息在计划回收窗口(第%s章)之前出现于正文"
                                    % (info["code"], start)})
    return hits


def transition_plan(before_status: str, action: str) -> tuple:
    """Convenience for tests/CLI: (after_status, ok, reason)."""
    fake = {"status": before_status}
    upd = advance(fake, 0, action)
    after = upd.get("status", before_status)
    ok, reason = validate_transition(before_status, after)
    return after, ok, reason
