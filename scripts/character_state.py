#!/usr/bin/env python3
"""Character state machine (deterministic).

Every major character carries a machine-readable state:

    location | goal | emotion | belief | relationship | knowledge | health |
    possession | status

The pre-upgrade system stored this as a sentence a human typed into a Markdown
table cell ("李禾禾：决意离开"), which is unqueryable, un-diffable and silently
lost. Here it is a fixed-shape record that every chapter commits a BEFORE->AFTER
transition against, so "where is 李禾 in chapter 400" is a SELECT, not a guess.

This module contains no database access: pure functions, trivially unit-testable.
"""
from __future__ import annotations

FIELDS = ("location", "goal", "emotion", "belief", "knowledge", "health",
          "possession", "status", "relationship")

LIST_FIELDS = ("belief", "knowledge", "possession", "relationship")

# Irreversible-ish fields: a state that should not silently flip back.
# {field: {target_value: (values it may not come back from)}}
IRREVERSIBLE = {
    "status": {"alive": ("dead", "destroyed"), "intact": ("destroyed",)},
}

DEFAULT_STATE = {
    "location": None, "goal": None, "emotion": None, "belief": [],
    "knowledge": [], "health": None, "possession": [], "status": "alive",
    "relationship": [],
}


def empty_state(name: str = "") -> dict:
    st = dict(DEFAULT_STATE)
    st["belief"] = []
    st["knowledge"] = []
    st["possession"] = []
    st["relationship"] = []
    st["name"] = name
    return st


def normalize(state: dict) -> dict:
    out = empty_state(state.get("name") or "")
    for f in FIELDS:
        if f in state and state[f] is not None:
            out[f] = state[f]
    for f in LIST_FIELDS:
        val = out.get(f)
        if val is None:
            out[f] = []
        elif isinstance(val, str):
            out[f] = [val] if val else []
    return out


def validate(state: dict) -> list:
    """Shape validation. Returns a list of human-readable problems."""
    issues = []
    if not isinstance(state, dict):
        return ["人物状态不是对象：%r" % type(state).__name__]
    for f in LIST_FIELDS:
        if f in state and state[f] is not None and not isinstance(state[f], (list, tuple)):
            issues.append("字段 %s 必须是数组，实际为 %s" % (f, type(state[f]).__name__))
    for entry in state.get("knowledge") or []:
        if isinstance(entry, dict):
            if not entry.get("fact_code"):
                issues.append("knowledge 条目缺少 fact_code：%r" % entry)
        elif not isinstance(entry, str):
            issues.append("knowledge 条目类型非法：%r" % type(entry).__name__)
    for entry in state.get("possession") or []:
        if not isinstance(entry, (str, dict)):
            issues.append("possession 条目类型非法：%r" % type(entry).__name__)
    for f in ("location", "goal", "emotion", "health", "status"):
        val = state.get(f)
        if val is not None and not isinstance(val, str):
            issues.append("字段 %s 必须是字符串，实际为 %s" % (f, type(val).__name__))
    return issues


def check_transition(field: str, before, after) -> tuple:
    """Return (ok, reason). Guard rails only; unknown values stay allowed."""
    if field not in FIELDS:
        return False, "未知人物状态字段：%s" % field
    if after is None:
        return False, "缺少 after 值（不做任何修改，记录 warning）"
    if field == "status":
        if isinstance(before, str) and isinstance(after, str):
            blocked = IRREVERSIBLE.get("status", {}).get(after)
            if blocked and before in blocked:
                return False, ("人物状态 %s -> %s 属于不可逆变化，需要显式复活/找回事件"
                               % (before, after))
    if field in LIST_FIELDS and not isinstance(after, (list, tuple)):
        return False, "字段 %s 的 after 必须是数组" % field
    return True, ""


def apply_change(state: dict, change: dict) -> tuple:
    """Apply one {character, field, before, after} transition.

    Returns (new_state, applied_dict_or_None, warning_or_None).
    """
    field = change.get("field")
    after = change.get("after")
    effective_before = state.get(field)
    if effective_before is None and change.get("before") is not None:
        effective_before = change["before"]
    ok, reason = check_transition(field, effective_before, after)
    if not ok:
        return state, None, {"code": "STATE_TRANSITION_REJECTED",
                             "message": "%s：%s" % (change.get("character"), reason),
                             "field": field, "after": after}
    new = dict(state)
    before = effective_before
    if field == "knowledge":
        new["knowledge"] = _merge_knowledge(new.get("knowledge") or [], after)
    elif field in LIST_FIELDS:
        new[field] = list(after)
    else:
        new[field] = after
    return new, {"character": change.get("character"), "field": field,
                 "before": before, "after": new[field]}, None


def apply_changes(state: dict, changes, chapter: int = None) -> dict:
    """Batch application. Returns {state, applied, warnings}."""
    cur = normalize(state)
    applied, warnings = [], []
    for ch in changes or []:
        cur, ok, warn = apply_change(cur, ch)
        if ok:
            applied.append(ok)
        if warn:
            warnings.append(warn)
    return {"state": cur, "applied": applied, "warnings": warnings}


def diff_states(before: dict, after: dict) -> list:
    """Field-level BEFORE -> AFTER diff (the shape the brief and snapshot use)."""
    b = normalize(before)
    a = normalize(after)
    out = []
    for f in FIELDS:
        if b.get(f) != a.get(f):
            out.append({"field": f, "before": b.get(f), "after": a.get(f)})
    return out


def _merge_knowledge(current: list, incoming) -> list:
    out = list(current)
    seen = set()
    for entry in out:
        if isinstance(entry, dict):
            seen.add(entry.get("fact_code") or entry.get("fact"))
        else:
            seen.add(entry)
    for entry in incoming or []:
        key = entry.get("fact_code") or entry.get("fact") if isinstance(entry, dict) else entry
        if key in seen:
            continue
        seen.add(key)
        out.append(entry)
    return out


def knows(state: dict, fact_code: str) -> bool:
    for entry in state.get("knowledge") or []:
        if isinstance(entry, dict) and entry.get("fact_code") == fact_code:
            return True
        if entry == fact_code:
            return True
    return False


def grant_knowledge(state: dict, fact_code: str, chapter=None, source: str = "") -> dict:
    new = normalize(state)
    new["knowledge"] = _merge_knowledge(
        new.get("knowledge") or [],
        [{"fact_code": fact_code, "chapter": chapter, "source": source}])
    return new


def snapshot_fields(state: dict) -> dict:
    """Canonical, JSON-stable projection stored in chapter snapshots."""
    s = normalize(state)
    return {f: s.get(f) for f in FIELDS}


def validate_against_canon(state: dict, locations=(), characters=()) -> list:
    """Reference validation: a location/relationship target must exist in Canon.

    Conservative: unknown references are reported, never auto-created here.
    """
    issues = []
    loc = state.get("location")
    if loc and locations and loc not in set(locations):
        issues.append("人物 %s 的位置 %r 不在 Canon location 列表中"
                      % (state.get("name"), loc))
    for entry in state.get("relationship") or []:
        target = entry.get("target") if isinstance(entry, dict) else entry
        if target and characters and target not in set(characters):
            issues.append("人物 %s 的关系对象 %r 不在 Canon character 列表中"
                          % (state.get("name"), target))
    return issues
