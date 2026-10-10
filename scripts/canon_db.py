#!/usr/bin/env python3
"""Canon DB: the long-term memory of the novel.

WHY THIS EXISTS
---------------
The pre-upgrade system kept the book's memory in Markdown and re-read the first
N characters of it every round. That is a cache, not a database: past ~100
chapters the writer forgets early settings, character states, foreshadowing and
causality. This module is the fix.

LAYERING (the only rule that matters)
-------------------------------------
    canonical chapter text  >  canon delta (validated)  >  canon.db
                                                          >  derived summaries
                                                          >  Markdown views

`bible.md`, `plot-ledger.md` and `summaries/` are DERIVED VIEWS. The database is
the source of truth. Nothing here ever parses Markdown at runtime except
`outline.md`, which is a human-authored PLAN (not a derived view) and therefore
legitimately an input.

DETERMINISM
-----------
Everything in this module is deterministic Python + stdlib sqlite3. The model
never writes here directly: it proposes a Canon Delta, extract.py validates it,
and apply_delta() writes it inside a caller-owned transaction.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _common import (  # noqa: E402
    NarrativeError,
    atomic_write_json,
    atomic_write_text,
    paths,
    now_iso,
    read_text,
    sha256_text,
    tokenize,
    jaccard,
)

DDL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")

# Tables replayed from snapshots. Everything else (audits, warnings, plans, tx,
# rounds, canon_changes, gates) is operational log and is intentionally NOT
# rebuilt by a replay — verify_replay() reports exactly this scope.
CANON_TABLES = (
    "meta", "chapters", "entities", "character_states", "character_state_changes",
    "events", "relationships", "foreshadowings", "plot_threads", "world_rules",
    "knowledge", "items", "volumes", "arcs", "timeline", "scene_index",
)

# Logical projections used to prove that a replay reproduces the live canon.
# Timestamps, surrogate ids and insertion order are protocol details, not story
# state: a rebuild cannot and should not reproduce them byte for byte.
LOGICAL_QUERIES = {
    "entities": """
        SELECT kind, name, aliases, status, first_chapter, last_chapter, data
        FROM entities ORDER BY kind, name""",
    "character_states": """
        SELECT e.name, s.chapter, s.location, s.goal, s.emotion, s.belief, s.knowledge,
               s.health, s.possession, s.status, s.relationship
        FROM character_states s JOIN entities e ON e.id = s.entity_id ORDER BY e.name""",
    "character_state_changes": """
        SELECT e.name, c.chapter, c.field, c.before_value, c.after_value, c.source
        FROM character_state_changes c JOIN entities e ON e.id = c.entity_id
        ORDER BY e.name, c.chapter, c.field, COALESCE(c.before_value,'')""",
    "events": """
        SELECT COALESCE(code,''), chapter, kind, summary, participants, location,
               COALESCE(timeline_at,''), causes, effects, irreversible, data
        FROM events ORDER BY chapter, COALESCE(code,''), summary""",
    "relationships": """
        SELECT a.name, b.name, r.kind, r.status, COALESCE(r.since_chapter,0),
               COALESCE(r.until_chapter,0), r.notes
        FROM relationships r JOIN entities a ON a.id=r.from_entity
        JOIN entities b ON b.id=r.to_entity ORDER BY a.name, b.name, r.kind""",
    "foreshadowings": """
        SELECT code, title, description, status, kind, COALESCE(planted_chapter,0),
               COALESCE(planned_payoff_start,0), COALESCE(planned_payoff_end,0),
               COALESCE(resolved_chapter,0), COALESCE(last_touched_chapter,0),
               stages, leak_detected
        FROM foreshadowings ORDER BY code""",
    "plot_threads": """
        SELECT code, name, kind, status, COALESCE(opened_chapter,0),
               COALESCE(closed_chapter,0), goal, COALESCE(arc,'')
        FROM plot_threads ORDER BY code""",
    "world_rules": """
        SELECT code, text, COALESCE(first_chapter,0), immutable
        FROM world_rules ORDER BY code""",
    "knowledge": """
        SELECT knower_name, fact_code, fact_text, COALESCE(learned_chapter,0), source,
               confidence, secret
        FROM knowledge ORDER BY fact_code, knower_name""",
    "items": """
        SELECT i.code, i.name, COALESCE(e.name,''), COALESCE(i.location,''),
               COALESCE(i.state,'')
        FROM items i LEFT JOIN entities e ON e.id = i.holder_id ORDER BY i.code""",
    "volumes": """
        SELECT volume, COALESCE(start_chapter,0), COALESCE(end_chapter,0), goal,
               irreversible_change, main_conflict, turns, hook
        FROM volumes ORDER BY volume""",
    "arcs": """
        SELECT code, name, COALESCE(volume,0), goal, status FROM arcs ORDER BY code""",
    "timeline": """
        SELECT chapter, world_time, COALESCE(note,'') FROM timeline ORDER BY chapter""",
    "chapters": """
        SELECT number, chars, sha256, COALESCE(volume,0), COALESCE(arc,''),
               COALESCE(timeline_at,''), COALESCE(plan_id,''), audit_status, gates,
               commit_id
        FROM chapters ORDER BY number""",
    "scene_index": """
        SELECT chapter, scene_no, COALESCE(location,''), participants, conflict,
               summary, dialogue_function, signature
        FROM scene_index ORDER BY chapter, scene_no""",
}

KINDS = ("character", "location", "organization", "item")

FORESHADOW_STATES = ("PLANTED", "ACTIVE", "DEVELOPING", "PAYOFF_READY",
                     "RESOLVED", "STALE_LEAK")

THREAD_STATES = ("planned", "active", "dormant", "resolved", "abandoned")


# ================================================================ connection

def ddl() -> str:
    return read_text(DDL_PATH)


def connect(root: str, path: str = None) -> sqlite3.Connection:
    db_path = path or paths(root)["canon"]
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = sqlite3.connect(db_path, isolation_level=None, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(root: str) -> sqlite3.Connection:
    conn = connect(root)
    conn.executescript(ddl())
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
                 ("schema", "1"))
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
                 ("created_at", now_iso()))
    return conn


def db_exists(root: str) -> bool:
    return os.path.isfile(paths(root)["canon"])


def table_names(conn: sqlite3.Connection) -> list:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
        "ORDER BY name").fetchall()
    return [r["name"] for r in rows]


def schema_report(conn: sqlite3.Connection) -> list:
    """Table/column/index inventory — used by the acceptance report."""
    out = []
    for t in table_names(conn):
        cols = [{"name": r["name"], "type": r["type"], "notnull": r["notnull"],
                 "pk": r["pk"], "default": r["dflt_value"]}
                for r in conn.execute("PRAGMA table_info(%s)" % t).fetchall()]
        idx = []
        for r in conn.execute("PRAGMA index_list(%s)" % t).fetchall():
            cols_i = [c["name"] for c in
                      conn.execute("PRAGMA index_info(%s)" % r["name"]).fetchall()]
            idx.append({"name": r["name"], "unique": bool(r["unique"]), "columns": cols_i})
        out.append({"table": t, "columns": cols, "indexes": idx})
    return out


# ================================================================ identity

def new_id(prefix: str) -> str:
    return "%s-%s" % (prefix, uuid.uuid4().hex[:12])


def get_entity(conn: sqlite3.Connection, kind: str, name: str):
    if not name:
        return None
    row = conn.execute("SELECT * FROM entities WHERE kind=? AND name=?",
                       (kind, name)).fetchone()
    if row:
        return row
    # alias hit
    for r in conn.execute("SELECT * FROM entities WHERE kind=?", (kind,)).fetchall():
        try:
            aliases = json.loads(r["aliases"] or "[]")
        except ValueError:
            aliases = []
        if name in aliases:
            return r
    return None


def resolve_name(conn: sqlite3.Connection, kind: str, name: str) -> str:
    row = get_entity(conn, kind, name)
    return row["name"] if row else name


def ensure_entity(conn: sqlite3.Connection, kind: str, name: str, aliases=None,
                  status: str = None, chapter: int = None, data=None):
    """Idempotent upsert. Returns (row, created_bool).

    An entity is NEVER deleted here: orphan detection is validate_state.py's job.
    """
    if kind not in KINDS:
        raise NarrativeError("unknown entity kind: %s" % kind)
    if not name:
        raise NarrativeError("entity name must not be empty")
    row = get_entity(conn, kind, name)
    ts = now_iso()
    if row is None:
        conn.execute(
            "INSERT INTO entities(kind,name,aliases,status,first_chapter,last_chapter,"
            "data,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (kind, name, json.dumps(aliases or [], ensure_ascii=False),
             status or ("alive" if kind == "character" else "active"),
             chapter, chapter, json.dumps(data or {}, ensure_ascii=False), ts, ts))
        row = get_entity(conn, kind, name)
        if kind == "character":
            conn.execute(
                "INSERT OR IGNORE INTO character_states(entity_id, chapter, status, "
                "updated_at) VALUES(?,?,?,?)", (row["id"], chapter, "alive", ts))
        return row, True

    merged_aliases = set(json.loads(row["aliases"] or "[]")) | set(aliases or [])
    new_status = status or row["status"]
    conn.execute(
        "UPDATE entities SET aliases=?, status=?, last_chapter=COALESCE(?,last_chapter), "
        "updated_at=? WHERE id=?",
        (json.dumps(sorted(merged_aliases), ensure_ascii=False), new_status,
         chapter, ts, row["id"]))
    return get_entity(conn, kind, name), False


# ================================================================ change log

def log_change(conn: sqlite3.Connection, action: str, *, actor="system",
               entity_kind=None, entity_ref=None, field=None, before=None, after=None,
               reason="", source="system", chapter=None, commit_id=None) -> None:
    conn.execute(
        "INSERT INTO canon_changes(ts,actor,action,entity_kind,entity_ref,field,"
        "before_value,after_value,reason,source,chapter,commit_id) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (now_iso(), actor, action, entity_kind, entity_ref, field,
         _enc(before), _enc(after), reason, source, chapter, commit_id))


def add_warning(conn: sqlite3.Connection, code: str, message: str, *, chapter=None,
                severity="warning", entity_kind=None, entity_ref=None, data=None) -> None:
    conn.execute(
        "INSERT INTO warnings(ts,chapter,code,message,severity,entity_kind,entity_ref,data)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (now_iso(), chapter, code, message, severity, entity_kind, entity_ref,
         json.dumps(data or {}, ensure_ascii=False)))


def _enc(value):
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _dec(value, default=None):
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return default if default is not None else value


# ================================================================ character state

CHARACTER_FIELDS = ("location", "goal", "emotion", "belief", "knowledge",
                    "health", "possession", "status", "relationship")
JSON_FIELDS = ("belief", "knowledge", "possession", "relationship")


def get_character_state(conn: sqlite3.Connection, name: str) -> dict:
    row = get_entity(conn, "character", name)
    if row is None:
        return {}
    st = conn.execute("SELECT * FROM character_states WHERE entity_id=?",
                      (row["id"],)).fetchone()
    if st is None:
        return {"name": row["name"], "entity_id": row["id"]}
    out = {"name": row["name"], "entity_id": row["id"],
           "chapter": st["chapter"], "status": st["status"]}
    for f in CHARACTER_FIELDS:
        val = st[f]
        out[f] = _dec(val, [] if f in JSON_FIELDS else val)
    return out


def all_character_states(conn: sqlite3.Connection, names=None) -> list:
    rows = conn.execute(
        "SELECT e.name AS name, s.* FROM character_states s JOIN entities e "
        "ON e.id = s.entity_id ORDER BY e.name").fetchall()
    out = []
    for st in rows:
        if names is not None and st["name"] not in names:
            continue
        d = {"name": st["name"], "chapter": st["chapter"], "status": st["status"]}
        for f in CHARACTER_FIELDS:
            d[f] = _dec(st[f], [] if f in JSON_FIELDS else None)
        out.append(d)
    return out


def set_character_field(conn: sqlite3.Connection, name: str, field: str, after,
                        *, chapter: int, source="canon", commit_id=None,
                        before_override=None) -> dict:
    """Apply one BEFORE -> AFTER state transition.

    Conservatism rule: when `after` is None the field is left untouched and a
    warning is recorded instead. Uncertain automatic judgements never rewrite
    Canon.
    """
    if field not in CHARACTER_FIELDS:
        raise NarrativeError("unknown character field: %s" % field)
    row, _ = ensure_entity(conn, "character", name, chapter=chapter)
    cur = get_character_state(conn, name)
    before = cur.get(field)
    if before_override is not None:
        before = before_override
    if after is None:
        add_warning(conn, "UNCERTAIN_STATE_CHANGE",
                    "人物 %s 的 %s 无法确定，已保持原值（before=%r）" % (name, field, before),
                    chapter=chapter, entity_kind="character", entity_ref=name)
        return {"applied": False, "field": field, "before": before, "after": before}

    from character_state import check_transition as _check
    ok, reason = _check(field, before, after)
    if not ok:
        # An irreversible flip (dead -> alive) must come from a human correction
        # (`canon.py update`), never from an automatic extraction.
        add_warning(conn, "STATE_TRANSITION_REJECTED",
                    "人物 %s 的 %s：%s" % (name, field, reason),
                    chapter=chapter, severity="error", entity_kind="character",
                    entity_ref=name)
        return {"applied": False, "field": field, "before": before, "after": before}

    if field in JSON_FIELDS and not isinstance(after, (list, dict)):
        after = _dec(after, after)
    stored = json.dumps(after, ensure_ascii=False) if field in JSON_FIELDS else (
        after if isinstance(after, str) or after is None else str(after))

    conn.execute("UPDATE character_states SET %s=?, chapter=?, updated_at=? "
                 "WHERE entity_id=?" % field, (stored, chapter, now_iso(), row["id"]))
    conn.execute(
        "INSERT INTO character_state_changes(entity_id,chapter,field,before_value,"
        "after_value,source,commit_id) VALUES(?,?,?,?,?,?,?)",
        (row["id"], chapter, field, _enc(before), _enc(after), source, commit_id))
    log_change(conn, "state_change", entity_kind="character", entity_ref=name,
               field=field, before=before, after=after, chapter=chapter,
               source=source, commit_id=commit_id)
    return {"applied": True, "field": field, "before": before, "after": after}


def character_state_history(conn: sqlite3.Connection, name: str, limit: int = 50) -> list:
    row = get_entity(conn, "character", name)
    if row is None:
        return []
    rows = conn.execute(
        "SELECT * FROM character_state_changes WHERE entity_id=? ORDER BY chapter DESC, id DESC "
        "LIMIT ?", (row["id"], limit)).fetchall()
    return [{"chapter": r["chapter"], "field": r["field"],
             "before": _dec(r["before_value"]), "after": _dec(r["after_value"]),
             "source": r["source"], "commit_id": r["commit_id"]} for r in rows]


# ================================================================ events

def add_event(conn: sqlite3.Connection, ev: dict, *, commit_id=None) -> str:
    code = ev.get("code") or ("E%s" % uuid.uuid4().hex[:6])
    conn.execute(
        "INSERT OR REPLACE INTO events(code,chapter,kind,summary,participants,location,"
        "timeline_at,causes,effects,irreversible,data) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (code, int(ev.get("chapter") or 0), ev.get("kind") or "event",
         ev.get("summary") or "", json.dumps(ev.get("participants") or [], ensure_ascii=False),
         ev.get("location"), ev.get("timeline_at"),
         json.dumps(ev.get("causes") or [], ensure_ascii=False),
         json.dumps(ev.get("effects") or [], ensure_ascii=False),
         1 if ev.get("irreversible") else 0,
         json.dumps(ev.get("data") or {}, ensure_ascii=False)))
    return code


def get_event(conn: sqlite3.Connection, code: str):
    return conn.execute("SELECT * FROM events WHERE code=?", (code,)).fetchone()


def events_of_chapter(conn: sqlite3.Connection, chapter: int) -> list:
    rows = conn.execute("SELECT * FROM events WHERE chapter=? ORDER BY id",
                        (chapter,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        for f in ("participants", "causes", "effects", "data"):
            d[f] = _dec(d[f], [] if f != "data" else {})
        out.append(d)
    return out


# ================================================================ foreshadowing

def upsert_foreshadow(conn: sqlite3.Connection, item: dict, *, chapter: int = None,
                      commit_id=None) -> dict:
    code = item.get("code")
    if not code:
        raise NarrativeError("foreshadowing requires a code (e.g. F017)")
    row = conn.execute("SELECT * FROM foreshadowings WHERE code=?", (code,)).fetchone()
    status = item.get("status")
    if status is not None and status not in FORESHADOW_STATES:
        raise NarrativeError("illegal foreshadowing status %r for %s (legal: %s)"
                            % (status, code, "|".join(FORESHADOW_STATES)))
    before_status = row["status"] if row else None
    if row is None:
        conn.execute(
            "INSERT INTO foreshadowings(code,title,description,status,kind,planted_chapter,"
            "planned_payoff_start,planned_payoff_end,resolved_chapter,last_touched_chapter,"
            "stages,leak_detected,data) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (code, item.get("title") or "", item.get("description") or "",
             status or "PLANTED", item.get("kind") or "long",
             item.get("planted_chapter", chapter), item.get("planned_payoff_start"),
             item.get("planned_payoff_end"), item.get("resolved_chapter"),
             chapter, json.dumps(item.get("stages") or [], ensure_ascii=False),
             1 if item.get("leak_detected") else 0,
             json.dumps(item.get("data") or {}, ensure_ascii=False)))
        row = conn.execute("SELECT * FROM foreshadowings WHERE code=?", (code,)).fetchone()
    else:
        fields, vals = [], []
        for col in ("title", "description", "status", "kind", "planned_payoff_start",
                    "planned_payoff_end", "resolved_chapter"):
            if item.get(col) is not None:
                fields.append("%s=?" % col)
                vals.append(item[col])
        fields.append("last_touched_chapter=?")
        vals.append(chapter if chapter is not None else row["last_touched_chapter"])
        if item.get("stages") is not None:
            fields.append("stages=?")
            vals.append(json.dumps(item["stages"], ensure_ascii=False))
        if item.get("leak_detected") is not None:
            fields.append("leak_detected=?")
            vals.append(1 if item["leak_detected"] else 0)
        if item.get("data") is not None:
            fields.append("data=?")
            vals.append(json.dumps(item["data"], ensure_ascii=False))
        vals.append(code)
        conn.execute("UPDATE foreshadowings SET %s WHERE code=?" % ",".join(fields), vals)
        row = conn.execute("SELECT * FROM foreshadowings WHERE code=?", (code,)).fetchone()

    after_status = row["status"]
    if before_status != after_status:
        log_change(conn, "foreshadow_status", entity_kind="foreshadowing", entity_ref=code,
                   field="status", before=before_status, after=after_status,
                   chapter=chapter, commit_id=commit_id, source="canon")
    return dict(row)


def get_foreshadow(conn: sqlite3.Connection, code: str):
    return conn.execute("SELECT * FROM foreshadowings WHERE code=?", (code,)).fetchone()


def list_foreshadow(conn: sqlite3.Connection, statuses=None) -> list:
    rows = conn.execute("SELECT * FROM foreshadowings ORDER BY code").fetchall()
    out = []
    for r in rows:
        if statuses and r["status"] not in statuses:
            continue
        d = dict(r)
        d["stages"] = _dec(d["stages"], [])
        d["data"] = _dec(d["data"], {})
        out.append(d)
    return out


# ================================================================ plot threads

def upsert_thread(conn: sqlite3.Connection, item: dict, *, chapter: int = None,
                  commit_id=None) -> dict:
    code = item.get("code")
    if not code:
        raise NarrativeError("plot thread requires a code (e.g. T1)")
    status = item.get("status")
    if status is not None and status not in THREAD_STATES:
        raise NarrativeError("illegal thread status %r for %s (legal: %s)"
                            % (status, code, "|".join(THREAD_STATES)))
    row = conn.execute("SELECT * FROM plot_threads WHERE code=?", (code,)).fetchone()
    before = row["status"] if row else None
    if row is None:
        conn.execute(
            "INSERT INTO plot_threads(code,name,kind,status,opened_chapter,closed_chapter,"
            "goal,arc,data) VALUES(?,?,?,?,?,?,?,?,?)",
            (code, item.get("name") or code, item.get("kind") or "sub",
             status or "planned", item.get("opened_chapter", chapter),
             item.get("closed_chapter"), item.get("goal") or "", item.get("arc"),
             json.dumps(item.get("data") or {}, ensure_ascii=False)))
    else:
        fields, vals = [], []
        for col in ("name", "kind", "status", "closed_chapter", "goal", "arc"):
            if item.get(col) is not None:
                fields.append("%s=?" % col)
                vals.append(item[col])
        if fields:
            vals.append(code)
            conn.execute("UPDATE plot_threads SET %s WHERE code=?" % ",".join(fields), vals)
    row = conn.execute("SELECT * FROM plot_threads WHERE code=?", (code,)).fetchone()
    if before != row["status"]:
        log_change(conn, "thread_status", entity_kind="plot_thread", entity_ref=code,
                   field="status", before=before, after=row["status"], chapter=chapter,
                   commit_id=commit_id)
    return dict(row)


def list_threads(conn: sqlite3.Connection, statuses=None) -> list:
    rows = conn.execute("SELECT * FROM plot_threads ORDER BY code").fetchall()
    out = []
    for r in rows:
        if statuses and r["status"] not in statuses:
            continue
        d = dict(r)
        d["data"] = _dec(d["data"], {})
        out.append(d)
    return out


# ================================================================ world rules / knowledge / items

def upsert_world_rule(conn: sqlite3.Connection, item: dict, *, chapter: int = None,
                      commit_id=None) -> str:
    code = item.get("code")
    if not code:
        raise NarrativeError("world rule requires a code (e.g. R1)")
    row = conn.execute("SELECT * FROM world_rules WHERE code=?", (code,)).fetchone()
    text = item.get("text") or ""
    if row is None:
        conn.execute("INSERT INTO world_rules(code,text,first_chapter,immutable,data) "
                     "VALUES(?,?,?,?,?)",
                     (code, text, item.get("first_chapter", chapter),
                      1 if item.get("immutable", True) else 0,
                      json.dumps(item.get("data") or {}, ensure_ascii=False)))
        log_change(conn, "world_rule_add", entity_kind="world_rule", entity_ref=code,
                   after=text, chapter=chapter, commit_id=commit_id)
    elif text and text != row["text"]:
        if row["immutable"] and not item.get("allow_rewrite"):
            add_warning(conn, "IMMUTABLE_RULE_CONFLICT",
                        "世界规则 %s 被标记为不可变，新文本被拒绝：%s" % (code, text[:60]),
                        chapter=chapter, entity_kind="world_rule", entity_ref=code)
            return code
        conn.execute("UPDATE world_rules SET text=? WHERE code=?", (text, code))
        log_change(conn, "world_rule_rewrite", entity_kind="world_rule", entity_ref=code,
                   before=row["text"], after=text, chapter=chapter, commit_id=commit_id)
    return code


def add_knowledge(conn: sqlite3.Connection, item: dict, *, chapter: int = None,
                  commit_id=None) -> dict:
    knower = item.get("knower")
    fact = item.get("fact_code")
    if not knower or not fact:
        raise NarrativeError("knowledge requires knower + fact_code")
    row, _ = ensure_entity(conn, "character", knower, chapter=chapter)
    name = row["name"]
    existing = conn.execute("SELECT * FROM knowledge WHERE knower_name=? AND fact_code=?",
                            (name, fact)).fetchone()
    if existing:
        conn.execute("UPDATE knowledge SET learned_chapter=COALESCE(?,learned_chapter),"
                     "source=?, confidence=?, secret=? WHERE id=?",
                     (chapter, item.get("source") or existing["source"],
                      float(item.get("confidence", existing["confidence"])),
                      1 if item.get("secret", existing["secret"]) else 0, existing["id"]))
    else:
        conn.execute(
            "INSERT INTO knowledge(knower_id,knower_name,fact_code,fact_text,learned_chapter,"
            "source,confidence,secret,data) VALUES(?,?,?,?,?,?,?,?,?)",
            (row["id"], name, fact, item.get("fact_text") or "", chapter,
             item.get("source") or "", float(item.get("confidence", 1.0)),
             1 if item.get("secret") else 0,
             json.dumps(item.get("data") or {}, ensure_ascii=False)))
        log_change(conn, "knowledge_add", entity_kind="character", entity_ref=name,
                   field=fact, after=item.get("fact_text") or "", chapter=chapter,
                   commit_id=commit_id)
    return {"knower": name, "fact_code": fact}


def who_knows(conn: sqlite3.Connection, fact_code: str) -> list:
    rows = conn.execute("SELECT knower_name FROM knowledge WHERE fact_code=? "
                        "AND confidence>=0.5 AND knower_name != ''", (fact_code,)).fetchall()
    return [r["knower_name"] for r in rows]


def upsert_item(conn: sqlite3.Connection, item: dict, *, chapter: int = None,
                commit_id=None) -> dict:
    code = item.get("code")
    if not code:
        raise NarrativeError("item requires a code (e.g. I03)")
    holder = item.get("holder")
    holder_row = ensure_entity(conn, "character", holder, chapter=chapter)[0] if holder else None
    row = conn.execute("SELECT * FROM items WHERE code=?", (code,)).fetchone()
    if row is None:
        conn.execute("INSERT INTO items(code,name,holder_id,location,state,data) "
                     "VALUES(?,?,?,?,?,?)",
                     (code, item.get("name") or code,
                      holder_row["id"] if holder_row else None, item.get("location"),
                      item.get("state") or "",
                      json.dumps(item.get("data") or {}, ensure_ascii=False)))
        log_change(conn, "item_add", entity_kind="item", entity_ref=code,
                   after=item.get("name") or code, chapter=chapter, commit_id=commit_id)
    else:
        fields, vals = [], []
        if holder_row is not None:
            fields.append("holder_id=?")
            vals.append(holder_row["id"])
        for col in ("name", "location", "state"):
            if item.get(col) is not None:
                fields.append("%s=?" % col)
                vals.append(item[col])
        if fields:
            vals.append(code)
            conn.execute("UPDATE items SET %s WHERE code=?" % ",".join(fields), vals)
    return dict(conn.execute("SELECT * FROM items WHERE code=?", (code,)).fetchone())


def list_items(conn: sqlite3.Connection) -> list:
    rows = conn.execute(
        "SELECT i.*, e.name AS holder_name FROM items i LEFT JOIN entities e "
        "ON e.id = i.holder_id ORDER BY i.code").fetchall()
    return [dict(r) for r in rows]


def upsert_relationship(conn: sqlite3.Connection, item: dict, *, chapter: int = None,
                        commit_id=None) -> dict:
    a, b, kind = item.get("from"), item.get("to"), item.get("kind")
    if not a or not b or not kind:
        raise NarrativeError("relationship requires from + to + kind")
    ra, _ = ensure_entity(conn, "character", a, chapter=chapter)
    rb, _ = ensure_entity(conn, "character", b, chapter=chapter)
    row = conn.execute("SELECT * FROM relationships WHERE from_entity=? AND to_entity=? "
                       "AND kind=?", (ra["id"], rb["id"], kind)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO relationships(from_entity,to_entity,kind,status,since_chapter,"
            "until_chapter,notes) VALUES(?,?,?,?,?,?,?)",
            (ra["id"], rb["id"], kind, item.get("status") or "active",
             item.get("since_chapter", chapter), item.get("until_chapter"),
             item.get("notes") or ""))
        log_change(conn, "relationship_add", entity_kind="relationship",
                   entity_ref="%s->%s:%s" % (ra["name"], rb["name"], kind),
                   after=item.get("status") or "active", chapter=chapter, commit_id=commit_id)
    else:
        if item.get("status") or item.get("notes") is not None:
            conn.execute("UPDATE relationships SET status=COALESCE(?,status), "
                         "notes=COALESCE(?,notes), until_chapter=COALESCE(?,until_chapter) "
                         "WHERE id=?",
                         (item.get("status"), item.get("notes"),
                          item.get("until_chapter"), row["id"]))
            log_change(conn, "relationship_update", entity_kind="relationship",
                       entity_ref="%s->%s:%s" % (ra["name"], rb["name"], kind),
                       before=row["status"], after=item.get("status") or row["status"],
                       chapter=chapter, commit_id=commit_id)
    return dict(conn.execute("SELECT * FROM relationships WHERE id=?",
                             (row["id"] if row else 0,)).fetchone()
                or {"from": ra["name"], "to": rb["name"], "kind": kind})


def list_relationships(conn: sqlite3.Connection, name: str = None) -> list:
    rows = conn.execute(
        "SELECT r.*, a.name AS from_name, b.name AS to_name FROM relationships r "
        "JOIN entities a ON a.id=r.from_entity JOIN entities b ON b.id=r.to_entity "
        "ORDER BY a.name, b.name").fetchall()
    out = []
    for r in rows:
        if name and name not in (r["from_name"], r["to_name"]):
            continue
        out.append({"from": r["from_name"], "to": r["to_name"], "kind": r["kind"],
                    "status": r["status"], "since_chapter": r["since_chapter"],
                    "until_chapter": r["until_chapter"], "notes": r["notes"]})
    return out


# ================================================================ outline -> volumes/arcs

def sync_outline(conn: sqlite3.Connection, outline: dict, prog: dict) -> None:
    bounds = {int(b["volume"]): b for b in
              __import__("_common").volume_bounds(prog)}
    for v in outline.get("volumes", []):
        num = int(v["volume"])
        b = bounds.get(num, {})
        conn.execute(
            "INSERT OR REPLACE INTO volumes(volume,start_chapter,end_chapter,goal,"
            "irreversible_change,main_conflict,turns,hook,raw) VALUES(?,?,?,?,?,?,?,?,?)",
            (num, b.get("start_chapter"), b.get("end_chapter"), v.get("goal", ""),
             v.get("irreversible_change", ""), v.get("main_conflict", ""),
             v.get("turns", ""), v.get("hook", ""), v.get("raw", "")))
    for a in outline.get("arcs", []):
        conn.execute("INSERT OR REPLACE INTO arcs(code,name,volume,goal,status,data) "
                     "VALUES(?,?,?,?,?,?)",
                     (a["code"], a.get("name") or a["code"], a.get("volume"),
                      a.get("goal", ""), a.get("status", "planned"),
                      json.dumps(a.get("data") or {}, ensure_ascii=False)))


def get_volume(conn: sqlite3.Connection, volume: int) -> dict:
    row = conn.execute("SELECT * FROM volumes WHERE volume=?", (volume,)).fetchone()
    return dict(row) if row else {}


def volumes_all(conn: sqlite3.Connection) -> list:
    return [dict(r) for r in conn.execute("SELECT * FROM volumes ORDER BY volume").fetchall()]


# ================================================================ timeline

def set_timeline(conn: sqlite3.Connection, chapter: int, world_time: str,
                 note: str = "") -> None:
    prev = conn.execute("SELECT order_index FROM timeline ORDER BY order_index").fetchall()
    conn.execute("INSERT OR REPLACE INTO timeline(chapter,world_time,order_index,note) "
                 "VALUES(?,?,?,?)", (chapter, world_time, len(prev) + 1, note))


def timeline_all(conn: sqlite3.Connection) -> list:
    return [dict(r) for r in
            conn.execute("SELECT * FROM timeline ORDER BY order_index").fetchall()]


# ================================================================ scenes

def add_scene(conn: sqlite3.Connection, sc: dict, chapter: int) -> str:
    sig = ",".join(sorted(sc.get("participants") or [])) + "|" + (sc.get("location") or "") \
        + "|" + (sc.get("conflict") or "")
    conn.execute(
        "INSERT OR REPLACE INTO scene_index(chapter,scene_no,location,participants,conflict,"
        "summary,dialogue_function,signature) VALUES(?,?,?,?,?,?,?,?)",
        (chapter, int(sc.get("scene_no") or 1), sc.get("location"),
         json.dumps(sc.get("participants") or [], ensure_ascii=False),
         sc.get("conflict") or "", sc.get("summary") or "",
         sc.get("dialogue_function") or "", sig))
    return sig


def list_scenes(conn: sqlite3.Connection, chapter: int = None) -> list:
    if chapter is None:
        rows = conn.execute("SELECT * FROM scene_index ORDER BY chapter, scene_no").fetchall()
    else:
        rows = conn.execute("SELECT * FROM scene_index WHERE chapter=? ORDER BY scene_no",
                            (chapter,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["participants"] = _dec(d["participants"], [])
        out.append(d)
    return out


# ================================================================ delta application

_ENTITY_BUCKETS = {
    "characters": "character",
    "locations": "location",
    "organizations": "organization",
    "items": "item",
}


def _character_before(conn, names) -> dict:
    return {n: get_character_state(conn, n) for n in names}


def _foreshadow_before(conn, codes) -> dict:
    out = {}
    for c in codes:
        row = get_foreshadow(conn, c)
        out[c] = dict(row) if row else None
    return out


def _thread_before(conn, codes) -> dict:
    out = {}
    for c in codes:
        row = conn.execute("SELECT * FROM plot_threads WHERE code=?", (c,)).fetchone()
        out[c] = dict(row) if row else None
    return out


def apply_delta(conn: sqlite3.Connection, delta: dict, *, commit_id: str = None,
                actor: str = "canon") -> dict:
    """Apply a VALIDATED canon delta inside the caller's transaction.

    The caller owns BEGIN/COMMIT. Returns a report containing the canonical
    BEFORE -> EVENTS -> AFTER material for the chapter snapshot.

    Conservative by construction:
      * entities referenced by name are created on demand (they appear in prose);
      * a state transition with `after: null` is recorded as a WARNING and the
        old value is kept;
      * anything the extractor marked uncertain is written to `warnings`, never
        to Canon.
    """
    chapter = int(delta.get("chapter") or 0)
    warnings = list(delta.get("warnings") or [])
    touched_chars = set()
    touched_entities = []
    report = {"chapter": chapter, "commit_id": commit_id}

    def _touch(kind, name, aliases=None, status=None, data=None):
        row, was_created = ensure_entity(conn, kind, name, aliases=aliases,
                                        status=status, chapter=chapter, data=data)
        touched_entities.append({"kind": kind, "name": row["name"]})
        if was_created:
            created.append({"kind": kind, "name": row["name"]})
        if kind == "character":
            touched_chars.add(row["name"])
        return row

    # 1. entities -----------------------------------------------------
    created = []
    for bucket, kind in _ENTITY_BUCKETS.items():
        for ent in delta.get("entities", {}).get(bucket) or []:
            name = ent.get("name") or ent.get("code")
            _touch(kind, name, aliases=ent.get("aliases"), status=ent.get("status"),
                   data=ent.get("data"))

    # 2. events -------------------------------------------------------
    events = []
    for ev in delta.get("events") or []:
        ev = dict(ev)
        ev["chapter"] = chapter
        code = add_event(conn, ev, commit_id=commit_id)
        for p in ev.get("participants") or []:
            _touch("character", p)
        if ev.get("location"):
            _touch("location", ev["location"])
        events.append({"code": code, "kind": ev.get("kind") or "event",
                       "summary": ev.get("summary") or "",
                       "participants": [resolve_name(conn, "character", p)
                                        for p in (ev.get("participants") or [])],
                       "location": ev.get("location"),
                       "causes": ev.get("causes") or [],
                       "effects": ev.get("effects") or [],
                       "irreversible": bool(ev.get("irreversible"))})

    # 3. facts / relationships / items / rules ------------------------
    for item in delta.get("knowledge_added") or []:
        add_knowledge(conn, item, chapter=chapter, commit_id=commit_id)
        if item.get("knower"):
            touched_chars.add(resolve_name(conn, "character", item["knower"]))

    rel_changes = []
    for item in delta.get("relationships") or []:
        before = [r for r in list_relationships(conn, item.get("from"))
                  if r["to"] == item.get("to") and r["kind"] == item.get("kind")]
        upsert_relationship(conn, item, chapter=chapter, commit_id=commit_id)
        rel_changes.append({"from": item.get("from"), "to": item.get("to"),
                            "kind": item.get("kind"), "status": item.get("status"),
                            "notes": item.get("notes"),
                            "since_chapter": item.get("since_chapter", chapter),
                            "until_chapter": item.get("until_chapter"),
                            "before": before[0]["status"] if before else None})

    item_changes = []
    for item in delta.get("items") or []:
        before = conn.execute("SELECT * FROM items WHERE code=?",
                              (item.get("code"),)).fetchone()
        upsert_item(conn, item, chapter=chapter, commit_id=commit_id)
        item_changes.append({"code": item.get("code"), "name": item.get("name"),
                             "holder": item.get("holder"),
                             "location": item.get("location"),
                             "state": item.get("state"),
                             "before_holder_id": before["holder_id"] if before else None})

    for item in delta.get("world_rules") or []:
        upsert_world_rule(conn, item, chapter=chapter, commit_id=commit_id)

    for item in delta.get("plot_threads") or []:
        upsert_thread(conn, item, chapter=chapter, commit_id=commit_id)

    # 4. character state transitions ----------------------------------
    fcodes = [f.get("code") for f in (delta.get("foreshadowing") or []) if f.get("code")]
    tcodes = [t.get("code") for t in (delta.get("plot_threads") or []) if t.get("code")]
    for ch in delta.get("character_changes") or []:
        if ch.get("character"):
            touched_chars.add(resolve_name(conn, "character", ch["character"]))

    before_chars = _character_before(conn, sorted(touched_chars))
    before_fs = _foreshadow_before(conn, fcodes)
    before_tc = _thread_before(conn, tcodes)

    changes = []
    for ch in delta.get("character_changes") or []:
        name = resolve_name(conn, "character", ch.get("character"))
        field = ch.get("field")
        if field not in CHARACTER_FIELDS:
            add_warning(conn, "UNKNOWN_STATE_FIELD",
                        "未知人物状态字段 %r（人物 %s），已忽略" % (field, name),
                        chapter=chapter, entity_kind="character", entity_ref=name)
            continue
        res = set_character_field(conn, name, field, ch.get("after"), chapter=chapter,
                                  source=ch.get("source") or "canon",
                                  commit_id=commit_id,
                                  before_override=ch.get("before"))
        changes.append({"character": name, "field": field,
                        "before": res["before"], "after": res["after"],
                        "applied": res["applied"]})
        touched_chars.add(name)

    # 5. foreshadowing lifecycle --------------------------------------
    fs_changes = []
    for f in delta.get("foreshadowing") or []:
        code = f.get("code")
        if not code:
            continue
        item = dict(f)
        action = item.get("action")
        if action and not item.get("status"):
            from foreshadow import advance as _advance
            prev_row = get_foreshadow(conn, code)
            base = prev_row["status"] if prev_row else "PLANTED"
            implied = _advance({"status": base, "planted_chapter": None}, chapter, action)
            item["status"] = implied.get("status", base)
            if item["status"] == "RESOLVED":
                item["resolved_chapter"] = chapter
            if item["status"] == "STALE_LEAK":
                item["leak_detected"] = True
        upsert_foreshadow(conn, item, chapter=chapter, commit_id=commit_id)
        row = get_foreshadow(conn, code)
        prev = before_fs.get(code)
        # Carry the FULL after-state: a snapshot must be able to rebuild the row
        # on its own, without the live database to lean on.
        fs_changes.append({"code": code,
                           "before": (prev or {}).get("status"),
                           "after": row["status"],
                           "action": f.get("action") or "touch",
                           "note": f.get("note") or "",
                           "title": row["title"],
                           "description": row["description"],
                           "kind": row["kind"],
                           "planned_payoff_start": row["planned_payoff_start"],
                           "planned_payoff_end": row["planned_payoff_end"],
                           "resolved_chapter": row["resolved_chapter"],
                           "planted_chapter": row["planted_chapter"],
                           "last_touched_chapter": row["last_touched_chapter"],
                           "leak_detected": int(row["leak_detected"] or 0)})

    # 6. threads ------------------------------------------------------
    tc_changes = []
    for t in delta.get("plot_threads") or []:
        code = t.get("code")
        if not code:
            continue
        row = conn.execute("SELECT * FROM plot_threads WHERE code=?", (code,)).fetchone()
        prev = before_tc.get(code)
        tc_changes.append({"code": code,
                           "before": (prev or {}).get("status"),
                           "after": row["status"] if row else None,
                           "action": t.get("action") or "touch",
                           "name": row["name"] if row else None,
                           "kind": row["kind"] if row else None,
                           "goal": row["goal"] if row else None,
                           "opened_chapter": row["opened_chapter"] if row else None,
                           "closed_chapter": row["closed_chapter"] if row else None})

    # 7. scenes -------------------------------------------------------
    for sc in delta.get("scenes") or []:
        add_scene(conn, sc, chapter)

    # 8. timeline -----------------------------------------------------
    tl = delta.get("timeline") or {}
    if tl.get("world_time"):
        set_timeline(conn, chapter, tl["world_time"], tl.get("note") or "")

    after_chars = {n: get_character_state(conn, n) for n in sorted(touched_chars)}

    for w in warnings:
        add_warning(conn, w.get("code") or "EXTRACTOR_WARNING",
                    w.get("message") or "", chapter=chapter,
                    severity=w.get("severity") or "warning",
                    entity_kind=w.get("entity_kind"), entity_ref=w.get("entity_ref"))

    report.update({
        "entities_created": created,
        "entities_touched": touched_entities,
        "events": events,
        "character_changes": changes,
        "knowledge_added": delta.get("knowledge_added") or [],
        "relationship_changes": rel_changes,
        "item_changes": item_changes,
        "world_rule_changes": delta.get("world_rules") or [],
        "foreshadow_changes": fs_changes,
        "thread_changes": tc_changes,
        "scenes": delta.get("scenes") or [],
        "warnings": warnings,
        "uncertain": delta.get("uncertain") or [],
        "before_characters": before_chars,
        "after_characters": after_chars,
        "before_foreshadowing": {k: (v or {}).get("status") for k, v in before_fs.items()},
        "before_threads": {k: (v or {}).get("status") for k, v in before_tc.items()},
    })
    return report


# ================================================================ snapshots

def snapshot_path(root: str, chapter: int) -> str:
    return os.path.join(paths(root)["snapshots"], "ch-%04d.json" % chapter)


def base_snapshot_path(root: str, chapter: int) -> str:
    return os.path.join(paths(root)["snapshots"], "base-ch-%04d.db" % chapter)


def build_snapshot(root: str, report: dict, *, chapter: int, commit_id: str,
                   chapter_sha: str, chars: int, volume: int, arc=None,
                   timeline=None, summary: str = "", title: str = "",
                   gates=None, plan_id=None) -> dict:
    return {
        "schema": 1,
        "chapter": chapter,
        "title": title,
        "summary": summary,
        "commit_id": commit_id,
        "created_at": now_iso(),
        "chars": chars,
        "chapter_sha256": chapter_sha,
        "volume": volume,
        "arc": arc,
        "timeline": timeline or {},
        "plan_id": plan_id,
        "gates": gates or {},
        "before": {
            "characters": report.get("before_characters") or {},
            "foreshadowing": report.get("before_foreshadowing") or {},
            "plot_threads": report.get("before_threads") or {},
        },
        "events": report.get("events") or [],
        "after": {
            "characters": report.get("after_characters") or {},
        },
        "character_changes": report.get("character_changes") or [],
        "knowledge_added": report.get("knowledge_added") or [],
        "relationship_changes": report.get("relationship_changes") or [],
        "item_changes": report.get("item_changes") or [],
        "world_rule_changes": report.get("world_rule_changes") or [],
        "foreshadow_changes": report.get("foreshadow_changes") or [],
        "thread_changes": report.get("thread_changes") or [],
        "scenes": report.get("scenes") or [],
        "entities_created": report.get("entities_created") or [],
        "warnings": report.get("warnings") or [],
        "uncertain": report.get("uncertain") or [],
    }


def write_snapshot_file(root: str, payload: dict) -> str:
    path = snapshot_path(root, int(payload["chapter"]))
    atomic_write_json(path, payload)
    return path


def insert_snapshot_row(conn: sqlite3.Connection, payload: dict, path: str,
                        base_path: str = None) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO snapshots(id,chapter,commit_id,path,kind,base_path,"
        "chapter_sha256,created_at,summary) VALUES(?,?,?,?,?,?,?,?,?)",
        ("S%04d" % int(payload["chapter"]), int(payload["chapter"]),
         payload.get("commit_id") or "", path, "delta", base_path,
         payload.get("chapter_sha256"), payload.get("created_at") or now_iso(),
         json.dumps({"summary": payload.get("summary") or "",
                     "events": len(payload.get("events") or []),
                     "state_changes": len(payload.get("character_changes") or []),
                     "foreshadow": len(payload.get("foreshadow_changes") or [])},
                    ensure_ascii=False)))


def load_snapshots(root: str) -> list:
    """All canon snapshots on disk (chapter deltas + manual patches), ordered.

    Manual patches (human canon corrections made outside a chapter commit) carry
    the chapter that was current when they were made, so replaying the log
    reproduces the exact state a rollback target should have.
    """
    d = paths(root)["snapshots"]
    if not os.path.isdir(d):
        return []
    out = []
    import re as _re
    for name in sorted(os.listdir(d)):
        m = _re.fullmatch(r"ch-(\d{4,})\.json", name)
        if not m:
            continue
        payload = _dec(read_text(os.path.join(d, name)), None)
        if isinstance(payload, dict):
            payload.setdefault("kind", "delta")
            out.append(payload)
    for name in sorted(os.listdir(d)):
        if not _re.fullmatch(r"manual-.*\.json", name):
            continue
        payload = _dec(read_text(os.path.join(d, name)), None)
        if isinstance(payload, dict):
            payload["kind"] = "manual"
            out.append(payload)
    out.sort(key=lambda p: (int(p.get("chapter") or 0), str(p.get("created_at") or "")))
    return out


def diff_snapshots(a: dict, b: dict) -> dict:
    """Compare two chapter snapshots (version compare: `canon.py compare 3 9`)."""
    fa = {c["character"]: c for c in a.get("character_changes") or []}
    fb = {c["character"]: c for c in b.get("character_changes") or []}
    return {
        "from_chapter": a.get("chapter"), "to_chapter": b.get("chapter"),
        "chars_delta": int(b.get("chars") or 0) - int(a.get("chars") or 0),
        "characters_only_in_from": sorted(set(fa) - set(fb)),
        "characters_only_in_to": sorted(set(fb) - set(fa)),
        "state_before": {n: s.get("location") for n, s in
                         (a.get("before", {}).get("characters") or {}).items()},
        "state_after": {n: s.get("location") for n, s in
                        (b.get("after", {}).get("characters") or {}).items()},
        "foreshadow_from": {c["code"]: c.get("after") for c in a.get("foreshadow_changes") or []},
        "foreshadow_to": {c["code"]: c.get("after") for c in b.get("foreshadow_changes") or []},
    }


# ---------------------------------------------------------------- replay

def _apply_snapshot(conn: sqlite3.Connection, payload: dict) -> None:
    """Rebuild canon from one snapshot's AFTER side. Deterministic and
    idempotent; used by replay()/restore_to_chapter()/verify_replay().

    kind='manual' payloads are human corrections (canon.py update): they carry a
    `patch` instead of chapter events and never create a chapter row.
    """
    chapter = int(payload.get("chapter") or 0)
    commit_id = payload.get("commit_id") or "replay"

    if payload.get("kind") == "manual":
        _apply_manual_patch(conn, payload.get("patch") or {}, chapter, commit_id)
        return

    for ent in payload.get("entities_created") or []:
        ensure_entity(conn, ent.get("kind") or "character", ent.get("name"),
                      chapter=chapter)
    for ent in payload.get("entities_touched") or []:
        ensure_entity(conn, ent.get("kind") or "character", ent.get("name"),
                      chapter=chapter)

    for name, state in (payload.get("after", {}).get("characters") or {}).items():
        ensure_entity(conn, "character", name, chapter=chapter)
        current = get_character_state(conn, name)
        for field in CHARACTER_FIELDS:
            if field not in state:
                continue
            val = state.get(field)
            if val is None:
                continue
            # Only write fields that actually changed. Writing every projected
            # field would bump character_states.chapter for characters that were
            # merely present in the chapter, and the replay would then differ
            # from the live database on a purely bookkeeping column.
            if current.get(field) == val:
                continue
            row = get_entity(conn, "character", name)
            stored = json.dumps(val, ensure_ascii=False) if field in JSON_FIELDS else val
            conn.execute("UPDATE character_states SET %s=?, chapter=?, updated_at=? "
                         "WHERE entity_id=?" % field, (stored, chapter, now_iso(), row["id"]))

    # Reproduce the per-field change log exactly as it was recorded at commit time.
    for chg in payload.get("character_changes") or []:
        name = chg.get("character")
        row = get_entity(conn, "character", name)
        if row is None:
            continue
        conn.execute(
            "INSERT INTO character_state_changes(entity_id,chapter,field,before_value,"
            "after_value,source,commit_id) VALUES(?,?,?,?,?,?,?)",
            (row["id"], chapter, chg.get("field"), _enc(chg.get("before")),
             _enc(chg.get("after")), chg.get("source") or "canon", commit_id))

    for ev in payload.get("events") or []:
        for p in ev.get("participants") or []:
            ensure_entity(conn, "character", p, chapter=chapter)
        if ev.get("location"):
            ensure_entity(conn, "location", ev["location"], chapter=chapter)
        add_event(conn, {
            "code": ev.get("code"), "chapter": chapter, "kind": ev.get("kind"),
            "summary": ev.get("summary"), "participants": ev.get("participants"),
            "location": ev.get("location"), "causes": ev.get("causes"),
            "effects": ev.get("effects"), "irreversible": ev.get("irreversible"),
        }, commit_id=commit_id)

    for k in payload.get("knowledge_added") or []:
        try:
            add_knowledge(conn, k, chapter=chapter, commit_id=commit_id)
        except NarrativeError:
            pass

    for sc in payload.get("scenes") or []:
        add_scene(conn, sc, chapter)

    for f in payload.get("foreshadow_changes") or []:
        if not f.get("code"):
            continue
        item = {"code": f["code"], "status": f.get("after")}
        for src, dst in (("title", "title"), ("description", "description"),
                         ("kind", "kind"), ("planned_payoff_start", "planned_payoff_start"),
                         ("planned_payoff_end", "planned_payoff_end"),
                         ("planted_chapter", "planted_chapter"),
                         ("resolved_chapter", "resolved_chapter"),
                         ("last_touched_chapter", "last_touched_chapter"),
                         ("leak_detected", "leak_detected")):
            if f.get(src) is not None:
                item[dst] = f[src]
        upsert_foreshadow(conn, item, chapter=chapter, commit_id=commit_id)

    for t in payload.get("thread_changes") or []:
        if not t.get("code"):
            continue
        item = {"code": t["code"], "status": t.get("after")}
        for key in ("name", "kind", "goal", "opened_chapter", "closed_chapter"):
            if t.get(key) is not None:
                item[key] = t[key]
        upsert_thread(conn, item, chapter=chapter, commit_id=commit_id)

    for it in payload.get("item_changes") or []:
        if not it.get("code"):
            continue
        item = {"code": it["code"], "holder": it.get("holder"),
                "location": it.get("location")}
        if it.get("name"):
            item["name"] = it["name"]
        if it.get("state") is not None:
            item["state"] = it["state"]
        upsert_item(conn, item, chapter=chapter, commit_id=commit_id)

    for r in payload.get("relationship_changes") or []:
        if not (r.get("from") and r.get("to") and r.get("kind")):
            continue
        item = {"from": r["from"], "to": r["to"], "kind": r["kind"],
                "status": r.get("status")}
        for key in ("notes", "since_chapter", "until_chapter"):
            if r.get(key) is not None:
                item[key] = r[key]
        upsert_relationship(conn, item, chapter=chapter, commit_id=commit_id)

    for w in payload.get("world_rule_changes") or []:
        if w.get("code"):
            upsert_world_rule(conn, w, chapter=chapter, commit_id=commit_id)

    tl = payload.get("timeline") or {}
    if tl.get("world_time"):
        set_timeline(conn, chapter, tl["world_time"], tl.get("note") or "")

    conn.execute(
        "INSERT OR REPLACE INTO chapters(number,title,file,chars,count_mode,sha256,volume,"
        "arc,timeline_at,plan_id,audit_status,gates,commit_id,committed_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (chapter, payload.get("title") or "", "chapters/ch-%04d.txt" % chapter,
         int(payload.get("chars") or 0), "no_ws", payload.get("chapter_sha256") or "",
         payload.get("volume"), payload.get("arc"),
         (payload.get("timeline") or {}).get("world_time"), payload.get("plan_id"),
         (payload.get("gates") or {}).get("audit", "pending"),
         json.dumps(payload.get("gates") or {}, ensure_ascii=False), commit_id,
         payload.get("created_at") or now_iso()))


def _apply_state_write(conn: sqlite3.Connection, name: str, field: str, after, *,
                       chapter: int, source: str, commit_id: str, before_hint=None) -> None:
    """Low-level state write that also reproduces the change log.

    Used by manual patches so that a replayed database has exactly the same
    character_state_changes rows as the live one.
    """
    if field not in CHARACTER_FIELDS or after is None:
        return
    row, _ = ensure_entity(conn, "character", name, chapter=chapter)
    cur = get_character_state(conn, name)
    before = cur.get(field) if before_hint is None else before_hint
    stored = json.dumps(after, ensure_ascii=False) if field in JSON_FIELDS else after
    conn.execute("UPDATE character_states SET %s=?, chapter=?, updated_at=? "
                 "WHERE entity_id=?" % field, (stored, chapter, now_iso(), row["id"]))
    conn.execute(
        "INSERT INTO character_state_changes(entity_id,chapter,field,before_value,"
        "after_value,source,commit_id) VALUES(?,?,?,?,?,?,?)",
        (row["id"], chapter, field, _enc(before), _enc(after), source, commit_id))


def _apply_manual_patch(conn: sqlite3.Connection, patch: dict, chapter: int,
                        commit_id: str) -> None:
    """Apply a human canon correction during replay."""
    for ent in patch.get("entities") or []:
        kind = ent.get("kind") or "character"
        name = ent.get("name")
        if not name:
            continue
        ensure_entity(conn, kind, name, aliases=ent.get("aliases"),
                      status=ent.get("status"), chapter=ent.get("first_chapter", 0),
                      data=ent.get("data"))
    for chg in patch.get("character_changes") or []:
        _apply_state_write(conn, chg.get("character"), chg.get("field"), chg.get("after"),
                           chapter=chapter, source=chg.get("source") or "manual",
                           commit_id=commit_id, before_hint=chg.get("before"))
    for name, fields in (patch.get("characters") or {}).items():
        for field, value in (fields or {}).items():
            _apply_state_write(conn, name, field, value, chapter=chapter,
                               source="human", commit_id=commit_id)
    for code, fields in (patch.get("foreshadowing") or {}).items():
        upsert_foreshadow(conn, dict(fields, code=code), chapter=chapter, commit_id=commit_id)
    for code, fields in (patch.get("threads") or {}).items():
        upsert_thread(conn, dict(fields, code=code), chapter=chapter, commit_id=commit_id)
    for item in patch.get("world_rules") or []:
        upsert_world_rule(conn, item, chapter=chapter, commit_id=commit_id)
    for item in patch.get("knowledge_added") or []:
        try:
            add_knowledge(conn, item, chapter=chapter, commit_id=commit_id)
        except NarrativeError:
            pass
    for item in patch.get("items") or []:
        if item.get("code"):
            upsert_item(conn, item, chapter=chapter, commit_id=commit_id)
    for item in patch.get("relationships") or []:
        try:
            upsert_relationship(conn, item, chapter=chapter, commit_id=commit_id)
        except NarrativeError:
            pass
    for name, aliases in (patch.get("aliases") or {}).items():
        row = get_entity(conn, "character", name)
        if row:
            conn.execute("UPDATE entities SET aliases=?, updated_at=? WHERE id=?",
                         (json.dumps(sorted(set(json.loads(row["aliases"] or "[]")) |
                                            set(aliases)), ensure_ascii=False),
                          now_iso(), row["id"]))
    for name, new_name in (patch.get("renames") or {}).items():
        row = get_entity(conn, "character", name)
        if row:
            conn.execute("UPDATE entities SET name=?, updated_at=? WHERE id=?",
                         (new_name, now_iso(), row["id"]))


def write_manual_patch(root: str, patch: dict, *, chapter: int, actor: str,
                       reason: str, commit_id: str = None) -> str:
    """Persist a human canon correction as a replayable snapshot."""
    cid = commit_id or new_id("MAN")
    payload = {"schema": 1, "kind": "manual", "chapter": int(chapter),
               "commit_id": cid, "created_at": now_iso(), "actor": actor,
               "reason": reason, "patch": patch}
    name = "manual-%s-%s.json" % (now_iso().replace(":", "").replace("-", ""), cid[-6:])
    path = os.path.join(paths(root)["snapshots"], name)
    os.makedirs(paths(root)["snapshots"], exist_ok=True)
    atomic_write_json(path, payload)
    return path


def insert_manual_snapshot_row(conn: sqlite3.Connection, payload_path: str,
                               payload: dict) -> str:
    sid = "M-%s" % payload["commit_id"][-10:]
    conn.execute(
        "INSERT OR REPLACE INTO snapshots(id,chapter,commit_id,path,kind,base_path,"
        "chapter_sha256,created_at,summary) VALUES(?,?,?,?,?,?,?,?,?)",
        (sid, int(payload["chapter"]), payload.get("commit_id") or "", payload_path,
         "manual", None, None, payload.get("created_at") or now_iso(),
         json.dumps({"reason": payload.get("reason") or "",
                     "actor": payload.get("actor") or ""}, ensure_ascii=False)))
    return sid


def replay(root: str, keep_chapter: int, target_db: str) -> dict:
    """Rebuild canon into `target_db` by replaying every snapshot <= keep_chapter.

    Deterministic and self-contained: chapter deltas + manual patches + the
    human-authored outline.md (which is the source of volume/arc structure) are
    enough to reconstruct the database without any operational log.
    """
    if os.path.isfile(target_db):
        os.remove(target_db)
    conn = sqlite3.connect(target_db, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(ddl())
    conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('schema','1')")
    conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('rebuilt_at',?)",
                 (now_iso(),))
    snaps = [p for p in load_snapshots(root) if int(p.get("chapter") or 0) <= keep_chapter]
    base_ch = _base_for(root, keep_chapter)
    base = base_snapshot_path(root, base_ch) if base_ch else None
    used_base = None
    # -1 means "no base snapshot": everything must be replayed, including the
    # chapter-0 manual patches produced by `canon.py import-md` / `update`.
    start_after = -1
    if base and os.path.isfile(base):
        conn.close()
        shutil.copyfile(base, target_db)
        conn = sqlite3.connect(target_db, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        start_after = int(base_ch)
        used_base = base
    conn.execute("BEGIN IMMEDIATE")
    for payload in snaps:
        if int(payload.get("chapter") or 0) <= start_after:
            continue
        _apply_snapshot(conn, payload)
    # volume/arc structure comes from the human-authored outline (a plan view)
    try:
        from _common import load_progress, parse_outline
        outline = parse_outline(os.path.join(paths(root)["state"], "outline.md"))
        sync_outline(conn, outline, load_progress(root))
    except Exception:
        pass
    conn.execute("COMMIT")
    conn.close()
    return {"target": target_db, "snapshots_applied": len(snaps), "base": used_base,
            "start_after": start_after}


def _base_for(root: str, keep_chapter: int):
    d = paths(root)["snapshots"]
    if not os.path.isdir(d):
        return None
    best = None
    import re as _re
    for name in os.listdir(d):
        m = _re.fullmatch(r"base-ch-(\d{4,})\.db", name)
        if not m:
            continue
        n = int(m.group(1))
        if n <= keep_chapter and (best is None or n > best):
            best = n
    return best


def write_base_snapshot(root: str, conn: sqlite3.Connection, chapter: int) -> str:
    path = base_snapshot_path(root, chapter)
    dest = sqlite3.connect(path)
    with dest:
        conn.backup(dest)
    dest.close()
    return path


LOG_TABLES = ("audits", "plans", "gates", "canon_changes", "warnings", "round_tasks",
              "transactions", "round_runs", "meta", "snapshots")


def copy_log_tables(src_db: str, dst_db: str, keep_chapter: int) -> dict:
    """After a rollback rebuild, restore the operational log for retained chapters.

    A replay reconstructs canon only. The audit/plan/traceability tables for
    chapters <= keep_chapter must be preserved, otherwise rolling back to chapter
    40 would silently erase the audit history of chapters 1-40.
    """
    if not os.path.isfile(src_db):
        return {"copied": {}}
    conn = sqlite3.connect(dst_db, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("ATTACH DATABASE ? AS old", (src_db,))
    copied = {}
    for t in LOG_TABLES:
        try:
            # NOTE: PRAGMA syntax is `PRAGMA schema.pragma(arg)`; writing
            # `PRAGMA table_info(schema.table)` is a syntax error ("near .").
            has_chapter = any(r["name"] == "chapter"
                              for r in conn.execute("PRAGMA main.table_info(%s)" % t).fetchall())
            if t in ("meta", "transactions", "round_runs") or not has_chapter:
                conn.execute("INSERT OR IGNORE INTO main.%s SELECT * FROM old.%s" % (t, t))
            else:
                conn.execute("DELETE FROM main.%s" % t)
                conn.execute("INSERT INTO main.%s SELECT * FROM old.%s WHERE chapter<=?"
                             % (t, t), (keep_chapter,))
            copied[t] = int(conn.execute("SELECT COUNT(*) c FROM main.%s" % t).fetchone()[0])
        except Exception as exc:  # noqa: BLE001 - one bad table must not abort a rollback
            copied[t] = "skipped: %s: %s" % (type(exc).__name__, exc)
    conn.execute("DETACH DATABASE old")
    conn.close()
    return {"copied": copied}


def _replace_with_retry(src: str, dst: str, attempts: int = 20) -> None:
    """os.replace, tolerating a lingering reader handle on Windows.

    A rollback swaps canon.db under the whole project. On Windows the swap fails
    with WinError 32 while any handle (another process, an open REPL, a GUI file
    viewer) still holds the file, so retry briefly before giving up loudly.
    """
    import time
    last = None
    for i in range(attempts):
        try:
            if os.path.isfile(dst):
                os.remove(dst)
            os.replace(src, dst)
            return
        except PermissionError as exc:
            last = exc
            time.sleep(0.05 * (i + 1))
    raise last


def restore_to_chapter(root: str, keep_chapter: int) -> dict:
    """Roll the whole canon back to the state after chapter `keep_chapter`.

    Deterministic: a fresh database is rebuilt from snapshots (optionally seeded
    from the newest base snapshot <= keep_chapter). The previous database is kept
    as `snapshots/pre-rollback-<ts>.db` so a rollback is itself reversible.
    """
    p = paths(root)
    live = p["canon"]
    keep = int(keep_chapter)
    backup = None
    if os.path.isfile(live):
        backup = os.path.join(p["snapshots"], "pre-rollback-%s.db" %
                              now_iso().replace(":", ""))
        os.makedirs(p["snapshots"], exist_ok=True)
        shutil.copyfile(live, backup)

    tmp = live + ".replay"
    info = replay(root, keep, tmp)
    log_copy = copy_log_tables(backup, tmp, keep) if (backup and os.path.isfile(backup)) \
        else {"copied": {}}
    for suffix in ("-wal", "-shm"):
        stale = tmp + suffix
        if os.path.isfile(stale):
            os.remove(stale)
    for suffix in ("-wal", "-shm"):
        stale = tmp + suffix
        if os.path.isfile(stale):
            os.remove(stale)
    _replace_with_retry(tmp, live)

    removed = 0
    d = p["snapshots"]
    import re as _re
    if os.path.isdir(d):
        for name in sorted(os.listdir(d)):
            m = _re.fullmatch(r"ch-(\d{4,})\.json", name)
            if m and int(m.group(1)) > keep:
                os.remove(os.path.join(d, name))
                removed += 1
            mb = _re.fullmatch(r"base-ch-(\d{4,})\.db", name)
            if mb and int(mb.group(1)) > keep:
                os.remove(os.path.join(d, name))
            if _re.fullmatch(r"manual-.*\.json", name):
                payload = _dec(read_text(os.path.join(d, name)), {}) or {}
                if int(payload.get("chapter") or 0) > keep:
                    os.remove(os.path.join(d, name))
                    removed += 1
    return {"ok": True, "to_chapter": keep, "backup": backup,
            "snapshots_applied": info["snapshots_applied"],
            "base_used": info["base"], "snapshot_files_removed": removed,
            "log_rows_copied": log_copy}


def verify_replay(root: str) -> dict:
    """Prove the snapshot log reproduces the live canon.

    Compares LOGICAL projections (story state), not raw rows: timestamps and
    surrogate ids are protocol details that a rebuild legitimately cannot and
    should not reproduce.
    """
    p = paths(root)
    tmp = p["canon"] + ".verify"
    snaps = load_snapshots(root)
    top = max([int(s.get("chapter") or 0) for s in snaps] or [0])
    info = replay(root, top, tmp)
    live = connect(root)
    other = sqlite3.connect(tmp)
    other.row_factory = sqlite3.Row
    diffs = []
    for table, sql in LOGICAL_QUERIES.items():
        try:
            a = [tuple(r) for r in live.execute(sql)]
        except sqlite3.Error as exc:
            diffs.append({"table": table, "error": "live: %s" % exc})
            continue
        try:
            b = [tuple(r) for r in other.execute(sql)]
        except sqlite3.Error as exc:
            diffs.append({"table": table, "error": "replay: %s" % exc})
            continue
        if a != b:
            sample = None
            for i, (x, y) in enumerate(zip(a, b)):
                if x != y:
                    sample = {"row": i, "live": repr(x)[:220], "replay": repr(y)[:220]}
                    break
            diffs.append({"table": table, "live_rows": len(a), "replay_rows": len(b),
                          "first_difference": sample})
    live.close()
    other.close()
    if os.path.isfile(tmp):
        os.remove(tmp)
    for suffix in ("-wal", "-shm"):
        if os.path.isfile(tmp + suffix):
            os.remove(tmp + suffix)
    return {"ok": not diffs, "chapters_replayed": top, "snapshots": len(snaps),
            "base_used": info["base"], "tables_compared": len(LOGICAL_QUERIES),
            "differences": diffs}


# ================================================================ plans / audits / gates

def save_plan(conn: sqlite3.Connection, chapter: int, payload: dict, *, kind="chapter",
              status="draft", source="rule", plan_id=None) -> str:
    pid = plan_id or new_id("PL")
    conn.execute(
        "INSERT OR REPLACE INTO plans(id,chapter,kind,payload,status,source,created_at,"
        "approved_at) VALUES(?,?,?,?,?,?,?,?)",
        (pid, chapter, kind, json.dumps(payload, ensure_ascii=False), status, source,
         now_iso(), now_iso() if status == "approved" else None))
    return pid


def get_plan(conn: sqlite3.Connection, chapter: int, kind="chapter"):
    row = conn.execute("SELECT * FROM plans WHERE chapter=? AND kind=? "
                       "ORDER BY created_at DESC LIMIT 1", (chapter, kind)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["payload"] = _dec(d["payload"], {})
    return d


def effective_plan(conn: sqlite3.Connection, chapter: int, kind="chapter"):
    """Newest plan for the chapter. There is no separate approval ceremony: a
    validated plan IS the approval (plan --check enforces the schema)."""
    return get_plan(conn, chapter, kind)


def record_audit(conn: sqlite3.Connection, chapter: int, level: int, *, file=None,
                 sha256=None, passed=False, blocking=None, advisories=None,
                 provider="rule", report_path=None) -> None:
    conn.execute(
        "INSERT INTO audits(chapter,level,file,sha256,pass,blocking,advisories,provider,"
        "report_path,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (chapter, level, file, sha256, 1 if passed else 0,
         json.dumps(blocking or [], ensure_ascii=False),
         json.dumps(advisories or [], ensure_ascii=False), provider, report_path,
         now_iso()))


def audit_for(conn: sqlite3.Connection, chapter: int, sha256: str = None, level: int = None):
    """Newest audit row for a chapter (optionally pinned to an exact draft hash)."""
    sql = "SELECT * FROM audits WHERE chapter=?"
    args = [chapter]
    if sha256 is not None:
        sql += " AND sha256=?"
        args.append(sha256)
    if level is not None:
        sql += " AND level=?"
        args.append(level)
    sql += " ORDER BY id DESC LIMIT 1"
    row = conn.execute(sql, args).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["pass"] = bool(d["pass"])
    d["blocking"] = _dec(d["blocking"], [])
    d["advisories"] = _dec(d["advisories"], [])
    return d


def set_gates(conn: sqlite3.Connection, chapter: int, gates: dict) -> None:
    cur = conn.execute("SELECT * FROM gates WHERE chapter=?", (chapter,)).fetchone()
    base = {"plan": "missing", "audit": "missing", "canon": "missing", "length": "missing"}
    if cur:
        base.update({k: cur[k] for k in ("plan", "audit", "canon", "length")})
        base["data"] = _dec(cur["data"], {})
    base.update({k: v for k, v in gates.items() if k in ("plan", "audit", "canon", "length")})
    data = base.pop("data", {})
    data.update(gates.get("data") or {})
    conn.execute(
        "INSERT OR REPLACE INTO gates(chapter,plan,audit,canon,length,data,updated_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (chapter, base["plan"], base["audit"], base["canon"], base["length"],
         json.dumps(data, ensure_ascii=False), now_iso()))


def get_gates(conn: sqlite3.Connection, chapter: int) -> dict:
    row = conn.execute("SELECT * FROM gates WHERE chapter=?", (chapter,)).fetchone()
    if row is None:
        return {"plan": "missing", "audit": "missing", "canon": "missing",
                "length": "missing", "data": {}}
    d = dict(row)
    d["data"] = _dec(d["data"], {})
    return d


# ================================================================ transactions / rounds

def tx_row(conn: sqlite3.Connection, tx_id: str, chapter: int, state: str,
           journal_path: str, steps=None) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO transactions(id,chapter,state,started_at,finished_at,steps,"
        "journal_path) VALUES(?,?,?,COALESCE((SELECT started_at FROM transactions WHERE id=?),?),"
        "?,?,?)",
        (tx_id, chapter, state, tx_id, now_iso(),
         now_iso() if state in ("committed", "rolled_back") else None,
         json.dumps(steps or [], ensure_ascii=False), journal_path))


def open_transactions(conn: sqlite3.Connection) -> list:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM transactions WHERE state NOT IN ('committed','rolled_back') "
        "ORDER BY started_at").fetchall()]


def start_round(conn: sqlite3.Connection, round_index: int, planned: list) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO round_runs(round_index,status,planned_chapters,started_at,"
        "closed_at,verdict,blockers,waivers,notes) VALUES(?,?,?,?,?,?,?,?,?)",
        (round_index, "open", json.dumps(planned), now_iso(), None, None, "[]", "[]", ""))
    for ch in planned:
        conn.execute(
            "INSERT OR IGNORE INTO round_tasks(round_index,chapter,stage,status,updated_at) "
            "VALUES(?,?,?,?,?)", (round_index, ch, "planned", "pending", now_iso()))


def get_round(conn: sqlite3.Connection, round_index: int):
    row = conn.execute("SELECT * FROM round_runs WHERE round_index=?",
                       (round_index,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["planned_chapters"] = _dec(d["planned_chapters"], [])
    d["blockers"] = _dec(d["blockers"], [])
    d["waivers"] = _dec(d["waivers"], [])
    return d


def latest_open_round(conn: sqlite3.Connection):
    row = conn.execute("SELECT * FROM round_runs WHERE status='open' "
                       "ORDER BY round_index DESC LIMIT 1").fetchone()
    if row is None:
        return None
    d = dict(row)
    d["planned_chapters"] = _dec(d["planned_chapters"], [])
    return d


def close_round(conn: sqlite3.Connection, round_index: int, verdict: str, *,
                blockers=None, waivers=None, status="closed", notes="") -> None:
    conn.execute(
        "UPDATE round_runs SET status=?, closed_at=?, verdict=?, blockers=?, waivers=?, "
        "notes=? WHERE round_index=?",
        (status, now_iso(), verdict, json.dumps(blockers or [], ensure_ascii=False),
         json.dumps(waivers or [], ensure_ascii=False), notes, round_index))


def round_task(conn: sqlite3.Connection, round_index: int, chapter: int, stage: str,
               status: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO round_tasks(round_index,chapter,stage,status,updated_at) "
        "VALUES(?,?,?,?,?)", (round_index, chapter, stage, status, now_iso()))


def round_tasks(conn: sqlite3.Connection, round_index: int) -> list:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM round_tasks WHERE round_index=? ORDER BY chapter, stage",
        (round_index,)).fetchall()]


# ================================================================ chapter row

def chapter_row(conn: sqlite3.Connection, number: int):
    row = conn.execute("SELECT * FROM chapters WHERE number=?", (number,)).fetchone()
    return dict(row) if row else None


def insert_chapter(conn: sqlite3.Connection, payload: dict) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO chapters(number,title,file,chars,count_mode,sha256,volume,"
        "arc,round_index,timeline_at,plan_id,audit_status,gates,commit_id,committed_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (int(payload["number"]), payload.get("title") or "",
         payload.get("file") or "chapters/ch-%04d.txt" % int(payload["number"]),
         int(payload.get("chars") or 0), payload.get("count_mode") or "no_ws",
         payload.get("sha256") or "", payload.get("volume"), payload.get("arc"),
         int(payload.get("round_index") or 0), payload.get("timeline_at"),
         payload.get("plan_id"), payload.get("audit_status") or "pending",
         json.dumps(payload.get("gates") or {}, ensure_ascii=False),
         payload.get("commit_id") or "", payload.get("committed_at") or now_iso()))


def chapters_all(conn: sqlite3.Connection) -> list:
    return [dict(r) for r in conn.execute("SELECT * FROM chapters ORDER BY number").fetchall()]


def chapter_summaries(conn: sqlite3.Connection, numbers=None) -> list:
    out = []
    for row in chapters_all(conn):
        if numbers is not None and row["number"] not in numbers:
            continue
        snap = load_snapshot(root_of(conn), row["number"])
        out.append({"number": row["number"],
                    "summary": (snap or {}).get("summary") or "",
                    "events": len((snap or {}).get("events") or []),
                    "chars": row["chars"],
                    "timeline_at": row["timeline_at"]})
    return out


def root_of(conn: sqlite3.Connection) -> str:
    """Recover the project root from the open database path (PRAGMA database_list)."""
    try:
        row = conn.execute("PRAGMA database_list").fetchone()
        db = row[2] if row else ""
    except sqlite3.Error:
        db = ""
    return os.path.dirname(os.path.dirname(os.path.abspath(db))) if db else "."


def load_snapshot(root: str, chapter: int):
    path = snapshot_path(root, chapter)
    if not os.path.isfile(path):
        return None
    return _dec(read_text(path), None)


def recount_committed(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) c FROM chapters").fetchone()["c"])
