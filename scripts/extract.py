#!/usr/bin/env python3
"""Canon Delta: schema, validator, and the two extractors.

THE CONTRACT
------------
    chapter text  -->  [ LLM or rules ]  -->  JSON Canon Delta
                  -->  validator  -->  warnings / refusal
                  -->  human-readable summary  -->  canon.db

A Canon Delta is the ONLY way chapter prose turns into Canon. It is a strict,
self-describing document; `validate_delta` is the gate. Two properties are
non-negotiable:

  1. Nothing unvalidated reaches the database. `novel_state.py commit` refuses to
     write Canon when validation fails (that is the `canon` gate).
  2. Uncertainty never mutates Canon. Any item marked `uncertain: true`, or with
     `confidence < 0.5`, is stripped out of the delta and recorded as a warning
     for a human to confirm.

`--delta <file>`      explicit agent-authored delta (recommended path)
`--llm command:...`   run an external model and parse its JSON response
provider=rule         deterministic extractor; never invents state changes
"""
from __future__ import annotations

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _common import NarrativeError, jaccard, read_text, tokenize  # noqa: E402
from character_state import FIELDS as CHARACTER_FIELDS  # noqa: E402
from foreshadow import FORESHADOW_STATES  # noqa: E402
from canon_db import THREAD_STATES  # noqa: E402

DELTA_SCHEMA_VERSION = 1

MIN_CONFIDENCE = 0.5

KINDS = ("character", "location", "organization", "item")

DELTA_SCHEMA = {
    "type": "object",
    "required": ["chapter"],
    "properties": {
        "schema": {"type": "integer"},
        "chapter": {"type": "integer"},
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "place": {"type": "string"},
        "arc": {"type": "string"},
        "timeline": {"type": "object", "properties": {
            "world_time": {"type": "string"}, "note": {"type": "string"}}},
        "entities": {"type": "object", "properties": {
            "characters": {"type": "array", "items": {"type": "object", "required": ["name"],
                                                      "properties": {
                                                          "name": {"type": "string"},
                                                          "aliases": {"type": "array",
                                                                      "items": {"type": "string"}},
                                                          "status": {"type": "string"}}}},
            "locations": {"type": "array", "items": {"type": "object", "required": ["name"]}},
            "organizations": {"type": "array", "items": {"type": "object", "required": ["name"]}},
            "items": {"type": "array", "items": {"type": "object", "required": ["name"]}}}},
        "character_changes": {"type": "array", "items": {
            "type": "object", "required": ["character", "field"],
            "properties": {"character": {"type": "string"},
                           "field": {"enum": list(CHARACTER_FIELDS)},
                           "before": {}, "after": {},
                           "confidence": {"type": "number"},
                           "uncertain": {"type": "boolean"},
                           "source": {"type": "string"}}}},
        "events": {"type": "array", "items": {
            "type": "object", "required": ["summary"],
            "properties": {"code": {"type": "string"}, "kind": {"type": "string"},
                           "summary": {"type": "string"},
                           "participants": {"type": "array", "items": {"type": "string"}},
                           "location": {"type": "string"},
                           "timeline_at": {"type": "string"},
                           "causes": {"type": "array", "items": {"type": "string"}},
                           "effects": {"type": "array", "items": {"type": "string"}},
                           "irreversible": {"type": "boolean"},
                           "confidence": {"type": "number"},
                           "uncertain": {"type": "boolean"}}}},
        "relationships": {"type": "array", "items": {
            "type": "object", "required": ["from", "to", "kind"]}},
        "items": {"type": "array", "items": {
            "type": "object", "required": ["code"],
            "properties": {"code": {"type": "string"}, "name": {"type": "string"},
                           "holder": {"type": "string"}, "location": {"type": "string"},
                           "state": {"type": "string"}}}},
        "knowledge_added": {"type": "array", "items": {
            "type": "object", "required": ["knower", "fact_code"],
            "properties": {"knower": {"type": "string"}, "fact_code": {"type": "string"},
                           "fact_text": {"type": "string"}, "source": {"type": "string"},
                           "secret": {"type": "boolean"},
                           "confidence": {"type": "number"},
                           "uncertain": {"type": "boolean"}}}},
        "world_rules": {"type": "array", "items": {
            "type": "object", "required": ["code", "text"],
            "properties": {"code": {"type": "string"}, "text": {"type": "string"},
                           "immutable": {"type": "boolean"}}}},
        "foreshadowing": {"type": "array", "items": {
            "type": "object", "required": ["code"],
            "properties": {"code": {"type": "string"}, "title": {"type": "string"},
                           "description": {"type": "string"},
                           "action": {"enum": ["plant", "touch", "advance", "develop",
                                               "ready", "resolve", "leak"]},
                           "status": {"enum": list(FORESHADOW_STATES)},
                           "kind": {"type": "string"},
                           "planned_payoff_start": {"type": "integer"},
                           "planned_payoff_end": {"type": "integer"},
                           "note": {"type": "string"},
                           "entities": {"type": "array", "items": {"type": "string"}},
                           "threads": {"type": "array", "items": {"type": "string"}},
                           "keywords": {"type": "array", "items": {"type": "string"}},
                           "uncertain": {"type": "boolean"}}}},
        "plot_threads": {"type": "array", "items": {
            "type": "object", "required": ["code"],
            "properties": {"code": {"type": "string"}, "name": {"type": "string"},
                           "kind": {"enum": ["main", "sub"]},
                           "action": {"enum": ["open", "advance", "close", "touch"]},
                           "status": {"enum": list(THREAD_STATES)},
                           "goal": {"type": "string"}}}},
        "scenes": {"type": "array", "items": {
            "type": "object", "required": ["summary"],
            "properties": {"scene_no": {"type": "integer"},
                           "location": {"type": "string"},
                           "participants": {"type": "array", "items": {"type": "string"}},
                           "conflict": {"type": "string"},
                           "summary": {"type": "string"},
                           "dialogue_function": {"type": "string"}}}},
        "warnings": {"type": "array"},
        "uncertain": {"type": "array"},
        "source": {"type": "string"},
    },
}

ARRAY_KEYS = ("events", "character_changes", "knowledge_added", "relationships",
              "items", "world_rules", "foreshadowing", "plot_threads", "scenes",
              "warnings", "uncertain")


# ================================================================ schema validator

def _validate(value, schema, path, errors, warnings):
    t = schema.get("type")
    if "enum" in schema:
        if value not in schema["enum"]:
            errors.append("%s: 值 %r 不在允许集合 %s 中" % (path, value, schema["enum"]))
            return
    if t == "object":
        if not isinstance(value, dict):
            errors.append("%s: 期望对象，实际 %s" % (path, type(value).__name__))
            return
        for req in schema.get("required", []):
            if req not in value or value[req] in (None, ""):
                errors.append("%s: 缺少必填字段 %r" % (path, req))
        for k, sub in (schema.get("properties") or {}).items():
            if k in value and value[k] is not None:
                _validate(value[k], sub, "%s.%s" % (path, k), errors, warnings)
    elif t == "array":
        if not isinstance(value, (list, tuple)):
            errors.append("%s: 期望数组，实际 %s" % (path, type(value).__name__))
            return
        item_schema = schema.get("items")
        if item_schema:
            for i, item in enumerate(value):
                _validate(item, item_schema, "%s[%d]" % (path, i), errors, warnings)
    elif t == "string":
        if not isinstance(value, str):
            errors.append("%s: 期望字符串，实际 %s" % (path, type(value).__name__))
    elif t == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            errors.append("%s: 期望整数，实际 %r" % (path, value))
    elif t == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            errors.append("%s: 期望数字，实际 %r" % (path, value))
    elif t == "boolean":
        if not isinstance(value, bool):
            errors.append("%s: 期望布尔，实际 %r" % (path, value))


def _is_uncertain(item) -> bool:
    if not isinstance(item, dict):
        return False
    if item.get("uncertain") is True:
        return True
    conf = item.get("confidence")
    if isinstance(conf, (int, float)) and not isinstance(conf, bool) and conf < MIN_CONFIDENCE:
        return True
    return False


def sanitize(delta: dict) -> tuple:
    """Strip uncertain material out of a delta. Returns (clean_delta, warnings).

    This is the mechanical expression of "不确定的自动判断默认不修改 Canon，只记录
    warning，由人工确认".
    """
    clean = dict(delta)
    warnings = list(delta.get("warnings") or [])
    for key in ARRAY_KEYS:
        if key in ("warnings", "uncertain"):
            continue
        items = delta.get(key)
        if not isinstance(items, list):
            continue
        kept = []
        for item in items:
            if _is_uncertain(item):
                warnings.append({
                    "code": "UNCERTAIN_ITEM_DROPPED",
                    "message": "不确定条目已从 Canon 写入中剔除（%s）：%s"
                               % (key, _preview(item)),
                    "severity": "warning",
                    "data": {"key": key, "item": item},
                })
            else:
                kept.append(item)
        clean[key] = kept
    for item in delta.get("uncertain") or []:
        if isinstance(item, dict):
            warnings.append({
                "code": item.get("code") or "UNCERTAIN_NOTE",
                "message": item.get("message") or _preview(item),
                "severity": item.get("severity") or "warning",
                "data": item,
            })
    clean["warnings"] = warnings
    clean["schema"] = DELTA_SCHEMA_VERSION
    return clean, warnings


def _preview(obj) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False)[:160]
    except (TypeError, ValueError):
        return repr(obj)[:160]


# ================================================================ canon-aware validation

def validate_delta(delta: dict, *, chapter: int = None, canon: dict = None,
                   allow_implicit_entities: bool = True) -> dict:
    """Full validation: schema + referential integrity + lifecycle legality.

    Returns {"ok": bool, "errors": [...], "warnings": [...], "delta": cleaned}.
    """
    errors, warnings = [], []
    if not isinstance(delta, dict):
        return {"ok": False, "errors": ["canon delta 不是 JSON 对象"], "warnings": [],
                "delta": {}}

    clean, extra_warnings = sanitize(delta)
    warnings.extend(extra_warnings)

    _validate(clean, DELTA_SCHEMA, "delta", errors, warnings)

    ch = clean.get("chapter")
    if chapter is not None and ch is not None and int(ch) != int(chapter):
        errors.append("canon delta 的 chapter=%s 与提交章节 %s 不一致" % (ch, chapter))

    canon = canon or {}
    characters = set(canon.get("characters") or [])
    locations = set(canon.get("locations") or [])
    organizations = set(canon.get("organizations") or [])
    items_known = set(canon.get("items") or [])
    fs_codes = set(canon.get("foreshadowing") or [])
    thread_codes = set(canon.get("threads") or [])

    declared = {"character": set(), "location": set(),
                "organization": set(), "item": set()}
    for bucket, kind in (("characters", "character"), ("locations", "location"),
                         ("organizations", "organization"), ("items", "item")):
        for ent in (clean.get("entities") or {}).get(bucket) or []:
            name = ent.get("name") if isinstance(ent, dict) else None
            if name:
                declared[kind].add(name)

    # ---- events
    seen_codes = set()
    for i, ev in enumerate(clean.get("events") or []):
        code = ev.get("code")
        if code:
            if code in seen_codes:
                errors.append("events[%d]: 事件编号重复 %s" % (i, code))
            seen_codes.add(code)
        if not (ev.get("summary") or "").strip():
            errors.append("events[%d]: summary 不能为空" % i)
        for p in ev.get("participants") or []:
            if p not in characters and p not in declared["character"]:
                if allow_implicit_entities:
                    warnings.append({"code": "IMPLICIT_ENTITY",
                                     "message": "事件参与者 %s 未在 Canon 或 entities 中声明，将按正文出现创建" % p})
                else:
                    errors.append("events[%d]: 参与者 %s 不存在" % (i, p))
        loc = ev.get("location")
        if loc and loc not in locations and loc not in declared["location"]:
            if allow_implicit_entities:
                warnings.append({"code": "IMPLICIT_ENTITY",
                                 "message": "事件地点 %s 未在 Canon 中声明，将按正文出现创建" % loc})
            else:
                errors.append("events[%d]: 地点 %s 不存在" % (i, loc))
        for cause in ev.get("causes") or []:
            if not isinstance(cause, str) or not cause:
                errors.append("events[%d]: causes 元素必须是非空事件编号" % i)

    # ---- character changes
    for i, chg in enumerate(clean.get("character_changes") or []):
        field = chg.get("field")
        if field not in CHARACTER_FIELDS:
            errors.append("character_changes[%d]: 非法字段 %r" % (i, field))
            continue
        if chg.get("after") is None:
            warnings.append({"code": "UNCERTAIN_STATE_CHANGE",
                             "message": "character_changes[%d] 缺少 after，将保持原值" % i})
        name = chg.get("character")
        if name and name not in characters and name not in declared["character"]:
            warnings.append({"code": "IMPLICIT_ENTITY",
                             "message": "状态变化对象 %s 未在 Canon 中声明" % name})

    # ---- knowledge
    for i, k in enumerate(clean.get("knowledge_added") or []):
        if not k.get("fact_code"):
            errors.append("knowledge_added[%d]: 缺少 fact_code" % i)
        if not k.get("knower"):
            errors.append("knowledge_added[%d]: 缺少 knower" % i)

    # ---- foreshadowing
    for i, f in enumerate(clean.get("foreshadowing") or []):
        code = f.get("code")
        if not code:
            errors.append("foreshadowing[%d]: 缺少 code" % i)
            continue
        if code not in fs_codes and f.get("action") not in ("plant", None):
            warnings.append({"code": "FORESHADOW_UNKNOWN",
                             "message": "伏笔 %s 不在登记表中且 action=%r；将按新埋处理"
                                        % (code, f.get("action"))})
        if f.get("status") and f["status"] not in FORESHADOW_STATES:
            errors.append("foreshadowing[%d]: 非法状态 %r" % (i, f["status"]))
        start, end = f.get("planned_payoff_start"), f.get("planned_payoff_end")
        if start is not None and end is not None and int(end) < int(start):
            errors.append("foreshadowing[%d]: 计划回收区间倒置 %s-%s" % (i, start, end))
        if f.get("action") == "plant" and not f.get("planned_payoff_start"):
            warnings.append({"code": "FORESHADOW_NO_WINDOW",
                             "message": "伏笔 %s 埋下但没有计划回收区间，将永远无法判定逾期" % code})

    # ---- threads
    for i, t in enumerate(clean.get("plot_threads") or []):
        if not t.get("code"):
            errors.append("plot_threads[%d]: 缺少 code" % i)
            continue
        if t.get("status") and t["status"] not in THREAD_STATES:
            errors.append("plot_threads[%d]: 非法状态 %r" % (i, t["status"]))
        if t["code"] not in thread_codes and t.get("action") not in ("open", None):
            warnings.append({"code": "THREAD_UNKNOWN",
                             "message": "线程 %s 不在登记表中且 action=%r" % (t["code"], t.get("action"))})

    # ---- timeline monotonicity
    tl = (clean.get("timeline") or {}).get("world_time")
    prev = canon.get("timeline_last")
    if tl and prev and str(tl) < str(prev):
        errors.append("时间线倒退：本章 world_time=%r 早于上一章 %r" % (tl, prev))
    if not tl:
        warnings.append({"code": "TIMELINE_NOT_ADVANCED",
                         "message": "本章未声明时间推进（world_time 缺失）"})

    # ---- places
    place = clean.get("place")
    if place and place not in locations and place not in declared["location"]:
        warnings.append({"code": "IMPLICIT_ENTITY", "message": "place %s 未在 Canon 中声明" % place})

    return {"ok": not errors, "errors": errors, "warnings": warnings, "delta": clean}


def load_delta(path: str) -> dict:
    if not os.path.isfile(path):
        raise NarrativeError("canon delta file not found: %s" % path)
    try:
        return json.loads(read_text(path))
    except ValueError as exc:
        raise NarrativeError("canon delta is not valid JSON: %s" % exc)


# ================================================================ rule extractor

_NAME_TAG = re.compile(r'"([^"\n]{1,200})"([\u4e00-\u9fff]{2,4})(?:说|道|问|答|应|喊|叫)')
_QUOTE = re.compile(r'[“"]([^”"\n]{1,300})[”"]')


def _candidates_in_text(text: str, names) -> list:
    return [n for n in names if n in text]


def rule_extract(chapter: int, text: str, *, plan=None, canon=None,
                 state_source: str = "delta") -> dict:
    """Deterministic extraction. Never invents: unknown fields become warnings.

    What it CAN establish by construction:
      * which known characters / locations appear in the prose (string presence)
      * which named speakers are new (quote + 说/道/问 pattern)
      * one event per planned scene (or one chapter event)
      * the timeline value declared by the plan
    What it CANNOT establish:
      * how a character's inner state changed -> warning only, unless the caller
        explicitly opts into plan-derived state (`state_source="plan"`)
    """
    plan = plan or {}
    canon = canon or {}
    delta = {
        "schema": DELTA_SCHEMA_VERSION,
        "chapter": chapter,
        "source": "rule",
        "title": plan.get("title") or "",
        "summary": plan.get("chapter_goal") or "",
        "entities": {"characters": [], "locations": [], "organizations": [], "items": []},
        "character_changes": [],
        "events": [],
        "relationships": [],
        "items": [],
        "knowledge_added": [],
        "world_rules": [],
        "foreshadowing": [],
        "plot_threads": [],
        "scenes": [],
        "warnings": [],
        "uncertain": [],
    }

    known_chars = list(canon.get("characters") or [])
    known_locs = list(canon.get("locations") or [])
    present_chars = _candidates_in_text(text, known_chars)
    present_locs = _candidates_in_text(text, known_locs)
    plan_chars = list(plan.get("characters") or [])

    for name in sorted(set(present_chars) | set(plan_chars)):
        delta["entities"]["characters"].append({"name": name})
    for name in present_locs:
        delta["entities"]["locations"].append({"name": name})

    # new named speakers: appear at least twice, not already canon, not a pronoun
    discovered = {}
    for m in _NAME_TAG.finditer(text):
        name = m.group(2)
        discovered[name] = discovered.get(name, 0) + 1
    for name, count in sorted(discovered.items()):
        if name in known_chars or name in ("他们", "她们", "我们", "你们", "自己"):
            continue
        if count >= 2:
            delta["entities"]["characters"].append({"name": name})
            delta["warnings"].append({
                "code": "NEW_NAMED_SPEAKER",
                "message": "从对白标签发现新人物 %s（出现 %d 次），已登记为 Canon 实体" % (name, count),
            })
        else:
            delta["uncertain"].append({
                "code": "SINGLE_MENTION_NAME",
                "message": "疑似人名 %s 仅出现 %d 次，未登记为 Canon" % (name, count),
            })

    # scenes -> events
    scenes = plan.get("scenes") or []
    if scenes:
        for i, sc in enumerate(scenes, start=1):
            delta["scenes"].append({
                "scene_no": sc.get("scene_no") or i,
                "location": sc.get("location"),
                "participants": sc.get("participants") or [],
                "conflict": sc.get("conflict") or "",
                "summary": sc.get("summary") or "",
                "dialogue_function": sc.get("dialogue_function") or "",
            })
            delta["events"].append({
                "kind": "scene",
                "summary": sc.get("summary") or (sc.get("conflict") or ""),
                "participants": sc.get("participants") or [],
                "location": sc.get("location") or plan.get("location"),
                "irreversible": bool(sc.get("irreversible")),
            })
    else:
        first = _first_sentence(text)
        delta["events"].append({
            "kind": "chapter",
            "summary": first or "（正文未提供可解析的事件摘要）",
            "participants": sorted(set(present_chars) | set(plan_chars)),
            "location": plan.get("location") or (present_locs[0] if present_locs else None),
        })

    tl = (plan.get("timeline") or {}).get("world_time") or plan.get("world_time")
    if tl:
        delta["timeline"] = {"world_time": tl, "note": plan.get("timeline_note") or ""}
    else:
        delta["warnings"].append({"code": "TIMELINE_NOT_ADVANCED",
                                 "message": "计划未声明本章时间推进"})

    # state changes: plan-derived only when explicitly allowed
    proposed = plan.get("expected_state_changes") or plan.get("must_change") or []
    for chg in proposed:
        if state_source == "plan":
            item = dict(chg)
            item.setdefault("source", "plan")
            delta["character_changes"].append(item)
            delta["warnings"].append({
                "code": "PLAN_DERIVED_STATE_CHANGE",
                "message": "人物状态变化 %s.%s 来自计划（未经正文语义确认），已按源 plan 写入"
                           % (chg.get("character"), chg.get("field")),
            })
        else:
            delta["uncertain"].append({
                "code": "PLAN_STATE_CHANGE_UNVERIFIED",
                "message": "计划声明了 %s 的 %s 变化，但规则提取器无法从正文确认；"
                           "已记录 warning，未修改 Canon（提供 --delta 或 --llm 以确认）"
                           % (chg.get("character"), chg.get("field")),
                "data": chg,
            })

    # foreshadowing: only lifecycle *touches* that the plan declares explicitly
    for f in plan.get("foreshadowing_actions") or []:
        delta["foreshadowing"].append(f)

    summary_bits = [e["summary"] for e in delta["events"] if e.get("summary")]
    delta["summary"] = (plan.get("summary") or "；".join(summary_bits))[:400]
    return delta


def _first_sentence(text: str) -> str:
    body = re.sub(r"\s+", "", text or "")
    m = re.search(r"^(.{6,80}?[。！？])", body)
    return m.group(1) if m else body[:60]


# ================================================================ orchestration

def delta_path_for(root: str, chapter: int) -> str:
    from _common import paths
    return os.path.join(paths(root)["plans"], "ch-%04d.delta.json" % chapter)


def discover_delta(root: str, chapter: int) -> str:
    """Look for an agent-authored delta next to the chapter's plan/draft."""
    from _common import paths
    p = paths(root)
    for cand in (os.path.join(p["plans"], "ch-%04d.delta.json" % chapter),
                 os.path.join(p["drafts"], "ch-%04d.delta.json" % chapter),
                 os.path.join(p["state"], "deltas", "ch-%04d.json" % chapter)):
        if os.path.isfile(cand):
            return cand
    return ""


def human_summary(delta: dict) -> str:
    """Derive the human-readable summary FROM the delta (not the other way round)."""
    bits = []
    if delta.get("summary"):
        bits.append(delta["summary"])
    events = delta.get("events") or []
    if events:
        bits.append("事件：" + "；".join(e.get("summary") or "" for e in events[:3]))
    changes = delta.get("character_changes") or []
    if changes:
        bits.append("状态：" + "；".join(
            "%s.%s %s→%s" % (c.get("character"), c.get("field"),
                             _short(c.get("before")), _short(c.get("after")))
            for c in changes[:4]))
    fs = delta.get("foreshadow_changes") or []
    if fs:
        bits.append("伏笔：" + "；".join("%s %s→%s" % (f.get("code"), f.get("before"),
                                                     f.get("after")) for f in fs[:4]))
    kn = delta.get("knowledge_added") or []
    if kn:
        bits.append("知识：" + "；".join("%s 得知 %s" % (k.get("knower"), k.get("fact_code"))
                                       for k in kn[:3]))
    return " | ".join(b for b in bits if b) or "（本章无可提取的结构化信息）"


def _short(value, limit: int = 16) -> str:
    if value is None:
        return "∅"
    s = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return s if len(s) <= limit else s[:limit] + "…"


# ---------------------------------------------------------------- plot repetition

def plot_repetition(delta: dict, prior_scenes: list, *, threshold: float = 0.62) -> list:
    """POSSIBLE_PLOT_REPETITION: same conflict/participants/location as an earlier
    scene even when the wording is completely different.

    n-gram repetition cannot see this: two chapters can share zero 8-grams and
    still be the same scene happening again.
    """
    hits = []
    for sc in delta.get("scenes") or []:
        cur = tokenize("%s %s %s" % (sc.get("conflict") or "", sc.get("summary") or "",
                                     sc.get("dialogue_function") or ""))
        for old in prior_scenes or []:
            old_tokens = tokenize("%s %s %s" % (old.get("conflict") or "",
                                                old.get("summary") or "",
                                                old.get("dialogue_function") or ""))
            same_cast = set(sc.get("participants") or []) == set(old.get("participants") or [])
            same_loc = (sc.get("location") or "") == (old.get("location") or "")
            sim = jaccard(cur, old_tokens)
            if sim >= threshold and same_cast and same_loc:
                hits.append({
                    "code": "POSSIBLE_PLOT_REPETITION",
                    "message": "第%d章第%s场与第%d章第%s场高度相似（%.2f）：同地点同参与人同冲突"
                               % (delta.get("chapter"), sc.get("scene_no"),
                                  old.get("chapter"), old.get("scene_no"), sim),
                    "similarity": round(sim, 3),
                    "chapter": old.get("chapter"),
                })
                break
    return hits


# ---------------------------------------------------------------- prompt

PROMPT_TEMPLATE = """你是长篇小说《{title}》的 Canon 提取器（Extractor）。你的唯一职责是把
已经写好的第 {chapter} 章正文转换成结构化 Canon Delta（JSON）。你不创作剧情，不润色文字。

铁律：
1. 只提取正文中**明确发生**的事实。凡是你不能从正文确定的字段，标 "uncertain": true 或
   "confidence": 0.4，系统会把它降级为 warning 而**不写入** Canon。
2. 禁止推测人物内心、禁止补充正文没有的动作、禁止把计划当成事实。
3. 人物状态变化必须给出 before（本章开始时）与 after（本章结束时）；未知就留 null。
4. 时间线必须单调不回退：上一章 world_time = {timeline_last}。
5. 输出**只有一个 JSON 对象**，不要解释、不要 Markdown 代码围栏。

当前 Canon 摘要（供引用一致性使用）：
{canon_summary}

本章计划（Chapter Plan，只是意图，不是事实）：
{plan_summary}

已登记伏笔：{foreshadow_codes}
已登记线程：{thread_codes}
本章不得提前泄露的信息：{forbidden}

第 {chapter} 章正文：
---
{text}
---

输出 JSON 结构（字段可省略，但不可改名）：
{{
  "chapter": {chapter},
  "summary": "一句话概括，事实层",
  "place": "本章主要地点（必须是 Canon 中已有 location 名）",
  "timeline": {{"world_time": "本章世界时间，需 >= 上一章", "note": ""}},
  "entities": {{"characters": [{{"name": "..."}}], "locations": [{{"name": "..."}}],
                "organizations": [], "items": [{{"name": "..."}}]}},
  "events": [{{"code": "E{chapter}-1", "kind": "decision|conflict|discovery|loss|gain",
              "summary": "...", "participants": ["..."], "location": "...",
              "causes": ["E7-2"], "effects": [], "irreversible": false,
              "uncertain": false}}],
  "character_changes": [{{"character": "...", "field":
      "location|goal|emotion|belief|knowledge|health|possession|status",
      "before": ..., "after": ...}}],
  "knowledge_added": [{{"knower": "...", "fact_code": "FACT-003",
                       "fact_text": "...", "source": "E{chapter}-1", "secret": true}}],
  "relationships": [{{"from": "...", "to": "...", "kind": "...", "status": "..."}}],
  "items": [{{"code": "I03", "name": "...", "holder": "...", "location": "...",
             "state": "..."}}],
  "world_rules": [{{"code": "R3", "text": "...", "immutable": true}}],
  "foreshadowing": [{{"code": "F017", "action":
      "plant|touch|develop|ready|resolve|leak", "status": "PLANTED|ACTIVE|DEVELOPING|PAYOFF_READY|RESOLVED",
      "note": "...", "planned_payoff_start": 40, "planned_payoff_end": 60,
      "entities": ["..."], "threads": ["T1"], "keywords": ["..."]}}],
  "plot_threads": [{{"code": "T1", "name": "...", "kind": "main|sub",
                    "action": "open|advance|close|touch", "status": "active"}}],
  "scenes": [{{"scene_no": 1, "location": "...", "participants": ["..."],
              "conflict": "本场冲突", "summary": "...", "dialogue_function": "试探|摊牌|交代"}}],
  "uncertain": [{{"code": "...", "message": "..."}}]
}}
"""


def build_prompt(*, title: str, chapter: int, text: str, canon_view: dict,
                 plan: dict, timeline_last: str = "", forbidden=(), ) -> str:
    fs = canon_view.get("foreshadowing") or []
    th = canon_view.get("threads") or []
    plan_summary = json.dumps({k: plan.get(k) for k in
                               ("chapter_goal", "core_conflict", "must_advance_main",
                                "must_advance_sub", "characters", "location",
                                "must_change", "forbidden_reveals", "scene_count")},
                              ensure_ascii=False, indent=1)
    canon_summary = json.dumps({
        "characters": canon_view.get("characters") or [],
        "locations": canon_view.get("locations") or [],
        "open_foreshadowing": canon_view.get("open_foreshadowing") or [],
        "threads": canon_view.get("thread_status") or {},
    }, ensure_ascii=False, indent=1)
    return PROMPT_TEMPLATE.format(
        title=title, chapter=chapter, text=text, canon_summary=canon_summary,
        plan_summary=plan_summary, foreshadow_codes=", ".join(map(str, fs)) or "（无）",
        thread_codes=", ".join(map(str, th)) or "（无）",
        forbidden=", ".join(map(str, forbidden)) or "（无）",
        timeline_last=timeline_last or "（第一章）")
