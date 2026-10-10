#!/usr/bin/env python3
"""Dynamic, query-driven context assembly: the four-layer memory.

THE POINT
---------
The pre-upgrade `next` did this:

    bible_digest = open("bible.md").read()[:4000]

That is a truncating cache read. It shows the same first 4000 characters in
chapter 5 and in chapter 900, so late-book writing silently loses early canon.

This module replaces it with a query:

    current task -> involved characters -> location -> conflict -> relevant
    foreshadowing -> query Canon DB -> query recent plot -> query current
    volume/arc -> assemble dynamic context

Only canon rows *related to this chapter* are pulled. A 200-character Bible
never enters the prompt; the 12 facts that matter do.

LAYERS
------
    L0  current context   : this chapter's task, goal, conflict, previous tail,
                            open questions, forbidden events
    L1  local plot memory : last N chapters (summaries, state changes, conflicts,
                            unresolved questions), current arc
    L2  book structure    : volume/arc goals, main+sub threads, character arcs,
                            unresolved conflicts, unrecovered foreshadowing with
                            payoff windows, world rules, volume-end irreversible
                            changes   <-- outline.md is a first-class input here
    L3  canon database    : entity states, knowledge boundaries, items,
                            relationships, events, foreshadow index
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import canon_db as db  # noqa: E402
import foreshadow as fs_mod  # noqa: E402
from _common import (  # noqa: E402
    chapter_path,
    find_volume_record,
    jaccard,
    parse_outline,
    paths,
    read_text,
    tail_chars,
    tokenize,
)

L1_WINDOW = 8           # chapters of local plot memory
L1_STATE_WINDOW = 10    # chapters of state-change history
L3_EVENT_WINDOW = 20    # chapters of event history queried for relevance


# ================================================================ canon view

def canon_view(conn, prog: dict, outline: dict = None) -> dict:
    """Flat, cheap-to-query projection of Canon. Passed to validators/extractors."""
    outline = outline if outline is not None else {}
    chars = [r["name"] for r in conn.execute(
        "SELECT name FROM entities WHERE kind='character' ORDER BY name").fetchall()]
    locs = [r["name"] for r in conn.execute(
        "SELECT name FROM entities WHERE kind='location' ORDER BY name").fetchall()]
    orgs = [r["name"] for r in conn.execute(
        "SELECT name FROM entities WHERE kind='organization' ORDER BY name").fetchall()]
    items = [r["code"] for r in conn.execute("SELECT code FROM items ORDER BY code").fetchall()]
    fs_rows = db.list_foreshadow(conn)
    th_rows = db.list_threads(conn)
    tl = db.timeline_all(conn)
    rules = [dict(r) for r in conn.execute(
        "SELECT code,text,immutable FROM world_rules ORDER BY code").fetchall()]
    return {
        "characters": chars,
        "locations": locs,
        "organizations": orgs,
        "items": items,
        "foreshadowing": [f["code"] for f in fs_rows],
        "open_foreshadowing": [f["code"] for f in fs_rows
                               if f["status"] not in fs_mod.TERMINAL],
        "threads": [t["code"] for t in th_rows],
        "thread_status": {t["code"]: t["status"] for t in th_rows},
        "world_rules": rules,
        "timeline_last": tl[-1]["world_time"] if tl else "",
        "chapter_count": db.recount_committed(conn),
        "outline": outline,
    }


# ================================================================ entity resolution

def resolve_entities(*, query: str = "", plan: dict = None, canon: dict = None,
                     chapter: int = None, limit: int = 12) -> dict:
    """Identify the characters/locations/items this chapter is about.

    Sources, in priority order: the plan's explicit declaration, then the query
    text (plan goal + conflict + previous chapter tail), then recency.
    """
    plan = plan or {}
    canon = canon or {}
    text = " ".join([query or "", plan.get("chapter_goal") or "",
                     plan.get("core_conflict") or "",
                     " ".join(plan.get("characters") or []),
                     " ".join(plan.get("must_advance_main") or []) if isinstance(
                         plan.get("must_advance_main"), list) else str(plan.get("must_advance_main") or "")])

    chars = []
    for name in plan.get("characters") or []:
        if name not in chars:
            chars.append(name)
    for name in canon.get("characters") or []:
        if name in text and name not in chars:
            chars.append(name)
    locs = []
    if plan.get("location"):
        locs.append(plan["location"])
    for name in canon.get("locations") or []:
        if name in text and name not in locs:
            locs.append(name)
    items = [code for code in (canon.get("items") or []) if code in text]
    orgs = [name for name in (canon.get("organizations") or []) if name in text]
    return {"characters": chars[:limit], "locations": locs[:limit],
            "items": items[:limit], "organizations": orgs[:limit]}


def _recent_character_names(conn, window: int = L1_WINDOW) -> list:
    rows = conn.execute(
        "SELECT DISTINCT e.name AS name FROM character_state_changes c "
        "JOIN entities e ON e.id=c.entity_id ORDER BY c.chapter DESC LIMIT 40").fetchall()
    return [r["name"] for r in rows]


# ================================================================ layers

def layer0(conn, root: str, chapter: int, plan: dict, status: dict) -> dict:
    prev = chapter - 1
    prev_text = ""
    if prev >= 1 and os.path.isfile(chapter_path(root, prev)):
        prev_text = read_text(chapter_path(root, prev))
    last_context = ""
    lc = paths(root)["last_context"]
    if os.path.isfile(lc):
        last_context = read_text(lc)
    return {
        "chapter": chapter,
        "task": "写出第 %d 章正文" % chapter,
        "scene_goal": (plan or {}).get("chapter_goal") or "",
        "core_conflict": (plan or {}).get("core_conflict") or "",
        "previous_chapter_tail": tail_chars(prev_text, 1200) if prev_text else "（这是第一章）",
        "questions_to_resolve": (plan or {}).get("questions_to_resolve") or [],
        "forbidden_events": (plan or {}).get("forbidden_events") or [],
        "last_context": last_context[:2000],
        "chars_tolerance": status.get("chars_tolerance"),
        "target_chars_this_chapter": status.get("chars_per_chapter"),
    }


def layer1(conn, root: str, chapter: int, *, window: int = L1_WINDOW) -> dict:
    """Local plot memory: recent chapters, recent state changes, recent conflicts,
    unresolved questions. NOT a text tail — a query over snapshots + canon."""
    start = max(1, chapter - window)
    numbers = list(range(start, chapter))
    recent = []
    for n in numbers:
        snap = db.load_snapshot(root, n)
        row = db.chapter_row(conn, n)
        if snap is None and row is None:
            continue
        recent.append({
            "chapter": n,
            "summary": (snap or {}).get("summary") or "",
            "events": [{"kind": e.get("kind"), "summary": e.get("summary"),
                        "participants": e.get("participants"),
                        "location": e.get("location"),
                        "irreversible": e.get("irreversible")}
                       for e in (snap or {}).get("events") or []],
            "state_changes": (snap or {}).get("character_changes") or [],
            "foreshadow_changes": (snap or {}).get("foreshadow_changes") or [],
            "timeline": ((snap or {}).get("timeline") or {}).get("world_time")
                        or (row or {}).get("timeline_at"),
        })
    changes = []
    for n in numbers:
        for row in conn.execute(
                "SELECT e.name AS name, c.field, c.before_value, c.after_value "
                "FROM character_state_changes c JOIN entities e ON e.id=c.entity_id "
                "WHERE c.chapter=? ORDER BY c.id", (n,)).fetchall():
            changes.append({"chapter": n, "character": row["name"], "field": row["field"],
                            "before": db._dec(row["before_value"]),
                            "after": db._dec(row["after_value"])})
    conflicts = []
    for r in recent:
        for e in r["events"]:
            if e.get("kind") in ("conflict", "decision", "loss", "gain", "discovery"):
                conflicts.append({"chapter": r["chapter"], "kind": e.get("kind"),
                                  "summary": e.get("summary")})
    unresolved = []
    for t in db.list_threads(conn):
        if t["status"] in ("active", "dormant", "planned"):
            unresolved.append({"code": t["code"], "name": t["name"], "kind": t["kind"],
                               "status": t["status"], "goal": t["goal"]})
    return {
        "window": [start, chapter - 1],
        "recent_chapters": recent,
        "recent_state_changes": changes[-40:],
        "recent_conflicts": conflicts[-12:],
        "unresolved_threads": unresolved,
        "recent_character_names": _recent_character_names(conn),
    }


def layer2(conn, root: str, chapter: int, prog: dict, outline: dict) -> dict:
    """Book-structure memory. outline.md participates here — it is parsed into
    volumes/arcs and every `next` now carries the volume/arc goal."""
    from _common import volume_bounds, volume_of
    vol = volume_of(prog, chapter)
    vrec = find_volume_record(outline, vol)
    if not vrec:
        vrec = db.get_volume(conn, vol)
    bounds = next((b for b in volume_bounds(prog) if b["volume"] == vol), {})
    arcs = [dict(r) for r in conn.execute(
        "SELECT * FROM arcs WHERE volume=? ORDER BY code", (vol,)).fetchall()]
    threads = db.list_threads(conn)
    fs_rows = db.list_foreshadow(conn)
    open_fs = [f for f in fs_rows if f["status"] not in fs_mod.TERMINAL]
    rules = [dict(r) for r in conn.execute(
        "SELECT code,text,immutable FROM world_rules ORDER BY code").fetchall()]
    char_arcs = [dict(r) for r in conn.execute(
        "SELECT e.name AS name, s.goal, s.status FROM character_states s "
        "JOIN entities e ON e.id=s.entity_id ORDER BY e.name").fetchall()]
    volumes = [dict(r) for r in conn.execute("SELECT * FROM volumes ORDER BY volume").fetchall()]
    return {
        "volume": vol,
        "volume_range": bounds,
        "volume_goal": vrec.get("goal") or "",
        "volume_irreversible_change": vrec.get("irreversible_change") or "",
        "volume_main_conflict": vrec.get("main_conflict") or "",
        "volume_hook": vrec.get("hook") or "",
        "arcs": arcs,
        "all_volumes": [{"volume": v["volume"], "goal": v.get("goal"),
                         "irreversible_change": v.get("irreversible_change")}
                        for v in volumes],
        "main_threads": [t for t in threads if t["kind"] == "main"],
        "sub_threads": [t for t in threads if t["kind"] != "main"],
        "open_foreshadowing": [{
            "code": f["code"], "title": f["title"], "status": f["status"],
            "kind": f["kind"], "planted_chapter": f["planted_chapter"],
            "planned_payoff_start": f["planned_payoff_start"],
            "planned_payoff_end": f["planned_payoff_end"],
            "phase": fs_mod.window_phase(f, chapter),
            "overdue": fs_mod.lifecycle_status(f, chapter)["overdue"],
        } for f in open_fs],
        "world_rules": rules,
        "character_arcs": char_arcs,
        "outline_parsed": bool(outline.get("parsed")),
        "outline_issues": outline.get("issues") or [],
    }


def layer3(conn, root: str, chapter: int, entities: dict, *, canon=None) -> dict:
    """Canon DB slice, keyed by the entities this chapter involves. This is the
    query that replaces "read the whole Bible"."""
    canon = canon or {}
    names = list(entities.get("characters") or [])
    states = db.all_character_states(conn, set(names)) if names else []
    if not names:
        states = db.all_character_states(conn)[:8]
    rels = []
    for n in names:
        rels.extend(db.list_relationships(conn, n))
    knowledge = []
    for st in states:
        for entry in st.get("knowledge") or []:
            if isinstance(entry, dict):
                knowledge.append({"knower": st["name"], "fact_code": entry.get("fact_code"),
                                  "chapter": entry.get("chapter"),
                                  "source": entry.get("source")})
            else:
                knowledge.append({"knower": st["name"], "fact_code": entry})
    facts = []
    for row in conn.execute("SELECT * FROM knowledge ORDER BY fact_code").fetchall():
        if names and row["knower_name"] not in names:
            continue
        facts.append({"knower": row["knower_name"], "fact_code": row["fact_code"],
                      "fact_text": row["fact_text"], "chapter": row["learned_chapter"],
                      "secret": bool(row["secret"])})
    items = [i for i in db.list_items(conn)
             if not names or (i.get("holder_name") in names) or (i.get("location") in
                                                                (entities.get("locations") or []))]
    events = []
    for n in range(max(1, chapter - L3_EVENT_WINDOW), chapter):
        for e in db.events_of_chapter(conn, n):
            if not names or set(e["participants"]) & set(names):
                events.append({"chapter": e["chapter"], "code": e["code"],
                               "kind": e["kind"], "summary": e["summary"],
                               "participants": e["participants"],
                               "location": e["location"], "causes": e["causes"],
                               "irreversible": bool(e["irreversible"])})
    return {
        "character_states": states,
        "relationships": rels,
        "knowledge": facts,
        "knowledge_by_character": knowledge,
        "items": items,
        "recent_events": events[-40:],
        "foreshadow_index": [{"code": f["code"], "status": f["status"],
                              "planned_payoff_start": f["planned_payoff_start"],
                              "planned_payoff_end": f["planned_payoff_end"]}
                             for f in db.list_foreshadow(conn)],
        "timeline": db.timeline_all(conn)[-12:],
    }


# ================================================================ assembly

def build_context(root: str, conn, prog: dict, chapter: int, *, plan: dict = None,
                  query: str = "", outline: dict = None, status: dict = None) -> dict:
    """The full four-layer context for one chapter. Pure query, no truncation."""
    os.makedirs(paths(root)["state"], exist_ok=True)
    if outline is None:
        outline = parse_outline(paths(root)["outline"])
    canon = canon_view(conn, prog, outline)
    entities = resolve_entities(query=query, plan=plan, canon=canon, chapter=chapter)
    if not entities["characters"]:
        entities["characters"] = _recent_character_names(conn)[:6]

    fs_rows = db.list_foreshadow(conn)
    thread_codes = [t["code"] for t in db.list_threads(conn)
                    if t["status"] in ("active", "planned", "dormant")]
    relevant_fs = fs_mod.select_relevant(
        fs_rows, chapter, entities=entities["characters"], thread_codes=thread_codes,
        text=(query or "") + json.dumps(plan or {}, ensure_ascii=False))

    l0 = layer0(conn, root, chapter, plan or {}, status or {})
    l1 = layer1(conn, root, chapter)
    l2 = layer2(conn, root, chapter, prog, outline)
    l3 = layer3(conn, root, chapter, entities, canon=canon)

    return {
        "chapter": chapter,
        "query": query or "",
        "L0_current": l0,
        "L1_local": l1,
        "L2_structure": l2,
        "L3_canon": l3,
        "entities": entities,
        "relevant_foreshadowing": relevant_fs,
        "forbidden_reveals": (plan or {}).get("forbidden_reveals") or [],
        "leak_risks": fs_mod.detect_leaks(fs_rows, query or "", chapter),
        "canon_digest": context_digest({
            "L2_structure": l2, "L3_canon": l3, "relevant_foreshadowing": relevant_fs,
            "entities": entities, "forbidden_reveals": (plan or {}).get("forbidden_reveals") or [],
            "chapter": chapter,
        }),
    }


def context_digest(ctx: dict) -> str:
    """Compact, deterministic text rendering of the context for the LLM prompt.

    This is what replaces `bible_digest = read()[:4000]`: it is generated from
    Canon rows selected for THIS chapter, so it stays the same size at chapter 5
    and chapter 900 while remaining accurate.
    """
    l2 = ctx.get("L2_structure") or {}
    l3 = ctx.get("L3_canon") or {}
    ent = ctx.get("entities") or {}
    out = []
    if l2:
        out.append("## 当前结构")
        out.append("- 卷 %s（第%s-%s章）：%s" % (
            l2.get("volume"), (l2.get("volume_range") or {}).get("start_chapter"),
            (l2.get("volume_range") or {}).get("end_chapter"), l2.get("volume_goal") or "（未填）"))
        if l2.get("volume_irreversible_change"):
            out.append("- 卷末不可逆变化：%s" % l2["volume_irreversible_change"])
        if l2.get("volume_main_conflict"):
            out.append("- 本卷主要冲突：%s" % l2["volume_main_conflict"])
        for t in (l2.get("main_threads") or [])[:6]:
            out.append("- 主线线程 %s《%s》状态=%s 目标=%s"
                       % (t.get("code"), t.get("name"), t.get("status"), t.get("goal") or "-"))
        for t in (l2.get("sub_threads") or [])[:6]:
            out.append("- 支线线程 %s《%s》状态=%s" % (t.get("code"), t.get("name"), t.get("status")))
        for r in (l2.get("world_rules") or [])[:12]:
            out.append("- 世界规则 %s：%s" % (r.get("code"), r.get("text")))
    if ent:
        out.append("## 本章涉及对象")
        out.append("- 人物：%s" % "、".join(ent.get("characters") or []) or "- 人物：（无）")
        out.append("- 地点：%s" % "、".join(ent.get("locations") or []))
    if l3.get("character_states"):
        out.append("## 人物当前状态（Canon）")
        for st in l3["character_states"]:
            out.append("- %s：位置=%s｜目标=%s｜情绪=%s｜健康=%s｜状态=%s"
                       % (st.get("name"), st.get("location"), st.get("goal"),
                          st.get("emotion"), st.get("health"), st.get("status")))
    if ctx.get("relevant_foreshadowing"):
        out.append("## 相关伏笔（按相关度排序，必须在正文中处理或明确回避）")
        for f in ctx["relevant_foreshadowing"][:8]:
            out.append("- %s《%s》状态=%s 计划回收=%s-%s%s：%s"
                       % (f.get("code"), f.get("title"), f.get("status"),
                          f.get("planned_payoff_start"), f.get("planned_payoff_end"),
                          "【逾期】" if f.get("overdue") else "",
                          "；".join(f.get("reasons") or [])))
    if ctx.get("forbidden_reveals"):
        out.append("## 本章禁止提前泄露")
        for x in ctx["forbidden_reveals"]:
            out.append("- %s" % x)
    return "\n".join(out) or "（Canon 为空）"


def context_to_json(ctx: dict) -> str:
    return json.dumps(ctx, ensure_ascii=False, indent=2)


# ================================================================ repetition helpers

def prior_scenes(conn, chapter: int, window: int = 40) -> list:
    start = max(1, chapter - window)
    rows = conn.execute(
        "SELECT * FROM scene_index WHERE chapter>=? AND chapter<? ORDER BY chapter, scene_no",
        (start, chapter)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["participants"] = db._dec(d["participants"], [])
        out.append(d)
    return out


def scene_similarity_to_history(conn, chapter: int, scenes: list) -> list:
    """Semantic (not textual) scene repetition check."""
    hist = prior_scenes(conn, chapter)
    hits = []
    for sc in scenes or []:
        cur = tokenize("%s %s %s" % (sc.get("conflict") or "", sc.get("summary") or "",
                                     sc.get("dialogue_function") or ""))
        for old in hist:
            sim = jaccard(cur, tokenize("%s %s %s" % (old["conflict"], old["summary"],
                                                       old["dialogue_function"])))
            if sim < 0.62:
                continue
            if set(sc.get("participants") or []) != set(old["participants"]):
                continue
            if (sc.get("location") or "") != (old["location"] or ""):
                continue
            hits.append({"code": "POSSIBLE_PLOT_REPETITION",
                         "message": "本章第%s场与第%d章第%d场结构重复（冲突/参与人/地点一致，相似度 %.2f）"
                                    % (sc.get("scene_no"), old["chapter"], old["scene_no"], sim),
                         "similarity": round(sim, 3)})
            break
    return hits
