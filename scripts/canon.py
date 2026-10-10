#!/usr/bin/env python3
"""Canon Manager CLI: inspect, correct, trace and roll back the story database.

    canon.py show                          overview of the whole canon
    canon.py character 李禾                 one character's full state + history
    canon.py event E17                     one event
    canon.py foreshadow [--code F017]      foreshadowing lifecycle + windows
    canon.py thread [--code T1]            plot threads
    canon.py knowledge [--fact FACT-003]   who knows what
    canon.py item [--code I03]             item custody
    canon.py state --at-chapter 40         reconstruct the world at chapter 40
    canon.py list --kind character         list entities
    canon.py update ...                    HUMAN CORRECTION (who/when/what/why logged)
    canon.py history --kind character --ref 李禾
    canon.py changes [--limit 40]          traceability log
    canon.py warnings [--all]              unresolved extractor/audit warnings
    canon.py snapshot --chapter 7          the chapter's canon snapshot
    canon.py compare 3 9                   diff two snapshots
    canon.py import-md                     bible.md -> Canon (then bible.md becomes derived)
    canon.py ddl [--out docs/DDL.sql]      canonical DDL
    canon.py schema                        tables/columns/indexes inventory
    canon.py verify-snapshots              prove the snapshot log reproduces canon.db
    canon.py rollback --to-chapter N       roll Canon back (also available via novel_state)

Every mutation goes through `update`, is applied inside one SQLite transaction, is
recorded in `canon_changes` (actor/time/what/before/after/why) and is persisted as
a replayable manual snapshot so a later rollback or replay still sees it.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import canon_db as db  # noqa: E402
import derived  # noqa: E402
import foreshadow as fs_mod  # noqa: E402
from _common import (  # noqa: E402
    NarrativeError,
    atomic_write_text,
    chapter_numbers,
    jprint,
    load_progress,
    now_iso,
    paths,
    read_text,
    volume_of,
)

KIND_TABLE = {
    "character": ("character_states", "character"),
    "location": ("entities", "location"),
    "organization": ("entities", "organization"),
    "item": ("items", "item"),
}


def _require(root: str):
    if not db.db_exists(root):
        raise NarrativeError("state/canon.db 不存在：先运行 novel_state.py init/migrate")
    return db.connect(root)


def _section_rows(text: str) -> dict:
    """Split a Markdown document into {section_key: [row_cells]} by heading keyword."""
    keys = (
        ("世界规则", "rules"), ("硬设定", "rules"),
        ("主要人物", "characters"),
        ("地理", "places"), ("组织", "places"),
        ("伏笔", "foreshadow"),
        ("剧情线程", "threads"), ("线程", "threads"),
        ("物品", "items"),
        ("知识", "knowledge"),
    )
    cur = None
    out = {}
    for raw in text.split("\n"):
        line = raw.strip()
        if line.startswith("#"):
            cur = None
            for kw, key in keys:
                if kw in line:
                    cur = key
                    out.setdefault(cur, [])
                    break
            continue
        if cur is None or not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if not cells or set(cells[0]) <= set("-: ") or not cells[0]:
            continue
        if cells[0] in ("编号", "姓名", "名称", "人物", "时间"):
            continue
        out[cur].append(cells)
    return out


def _as_int(value, default=None):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def import_bible_md(root: str, conn, actor: str = "human", convert_view: bool = True) -> dict:
    """Parse the human-authored bible.md into Canon. Idempotent."""
    path = paths(root)["bible"]
    if not os.path.isfile(path):
        return {"ok": False, "reason": "state/bible.md 不存在"}
    text = read_text(path)
    if derived.MARKER in text[:400]:
        return {"ok": True, "skipped": "bible.md 已是派生视图，无需导入"}
    sections = _section_rows(text)
    prog = load_progress(root)
    applied = {"rules": 0, "characters": 0, "places": 0, "foreshadow": 0,
               "threads": 0, "items": 0, "aliases": 0}
    # The import is a canon mutation outside a chapter commit, so it must be
    # replayable: everything it writes is also recorded as a manual patch.
    patch = {"entities": [], "characters": {}, "character_changes": [],
             "foreshadowing": {}, "threads": {}, "world_rules": [],
             "knowledge_added": [], "items": [], "relationships": [],
             "aliases": {}, "renames": {}}

    # keep a copy of the hand-written source before it becomes a derived view
    src_copy = os.path.join(paths(root)["state"], "bible.source.md")
    if not os.path.isfile(src_copy):
        atomic_write_text(src_copy, text)

    conn.execute("BEGIN IMMEDIATE")
    for row in sections.get("rules", []):
        code = row[0]
        if not code:
            continue
        rule = {"code": code, "text": row[1] if len(row) > 1 else "",
                "first_chapter": _as_int(row[2]) if len(row) > 2 else None,
                "immutable": (len(row) < 4 or "是" in row[3])}
        db.upsert_world_rule(conn, rule, chapter=0, commit_id="import-md")
        patch["world_rules"].append(dict(rule, allow_rewrite=True))
        applied["rules"] += 1
    for row in sections.get("characters", []):
        name = row[0]
        if not name:
            continue
        data = {"identity": row[1] if len(row) > 1 else "",
                "desire": row[2] if len(row) > 2 else "",
                "flaw": row[3] if len(row) > 3 else "",
                "destiny": row[4] if len(row) > 4 else ""}
        status = (row[5] if len(row) > 5 else "存活") or "存活"
        db.ensure_entity(conn, "character", name, chapter=0, data=data,
                         status="dead" if "死" in status else "alive")
        patch["entities"].append({"kind": "character", "name": name,
                                  "status": "dead" if "死" in status else "alive",
                                  "data": data, "first_chapter": 0})
        if data["desire"]:
            db.set_character_field(conn, name, "goal", data["desire"], chapter=0,
                                   source="import-md")
            patch.setdefault("character_changes", []).append(
                {"character": name, "field": "goal", "before": None,
                 "after": data["desire"], "source": "import-md"})
        applied["characters"] += 1
    for row in sections.get("places", []):
        name = row[0]
        if not name:
            continue
        kind = "organization" if (len(row) > 1 and "组织" in row[1]) else "location"
        note = row[2] if len(row) > 2 else ""
        db.ensure_entity(conn, kind, name, chapter=0, data={"note": note})
        patch["entities"].append({"kind": kind, "name": name, "data": {"note": note},
                                  "first_chapter": 0})
        applied["places"] += 1
    for row in sections.get("foreshadow", []):
        code = row[0]
        if not code:
            continue
        window = row[3] if len(row) > 3 else ""
        start = _as_int(str(window).split("-")[0])
        end = _as_int(str(window).split("-")[-1]) if "-" in str(window) else start
        status_raw = row[4] if len(row) > 4 else ""
        status = {"未回收": "PLANTED", "已回收": "RESOLVED", "回收": "RESOLVED",
                  "": "PLANTED"}.get(status_raw, "PLANTED")
        if status_raw in fs_mod.FORESHADOW_STATES:
            status = status_raw
        fs = {"code": code, "title": row[1] if len(row) > 1 else "", "status": status,
              "planted_chapter": _as_int(row[2]) if len(row) > 2 else None,
              "planned_payoff_start": start, "planned_payoff_end": end}
        db.upsert_foreshadow(conn, fs, chapter=0, commit_id="import-md")
        patch["foreshadowing"][code] = {k: v for k, v in fs.items() if k != "code"}
        applied["foreshadow"] += 1
    for row in sections.get("threads", []):
        code = row[0]
        if not code:
            continue
        status = (row[4] if len(row) > 4 else "planned") or "planned"
        status = {"进行中": "active", "未开始": "planned", "已结束": "resolved",
                  "": "planned"}.get(status, status if status in db.THREAD_STATES else "planned")
        th = {"code": code, "name": row[1] if len(row) > 1 else code,
              "kind": (row[2] if len(row) > 2 else "sub") or "sub",
              "goal": row[3] if len(row) > 3 else "", "status": status,
              "opened_chapter": 0}
        db.upsert_thread(conn, th, chapter=0, commit_id="import-md")
        patch["threads"][code] = {k: v for k, v in th.items() if k != "code"}
        applied["threads"] += 1
    db.log_change(conn, "import_bible_md", actor=actor, entity_kind="project",
                  entity_ref="bible.md", reason="把手工 bible.md 灌入 Canon",
                  after=applied)
    commit_id = db.new_id("MAN")
    patch_path = db.write_manual_patch(root=root, patch=patch, chapter=0, actor=actor,
                                      reason="bible.md 导入", commit_id=commit_id)
    db.insert_manual_snapshot_row(conn, patch_path,
                                  {"schema": 1, "kind": "manual", "chapter": 0,
                                   "commit_id": commit_id, "created_at": now_iso(),
                                   "actor": actor, "reason": "bible.md 导入",
                                   "patch": patch})
    conn.execute("COMMIT")

    if convert_view:
        _resync(root, conn)
    return {"ok": True, "applied": applied, "patch": patch_path,
            "sections": {k: len(v) for k, v in sections.items()},
            "source_copy": src_copy}


def _resync(root: str, conn) -> dict:
    from _common import save_progress
    prog = load_progress(root)
    prog["chapters_committed"] = len(chapter_numbers(root))
    save_progress(root, prog)
    return derived.write_views(root, conn, force_bible=True)


# ================================================================ read commands

def cmd_show(args) -> int:
    conn = _require(args.root)
    nums = chapter_numbers(args.root)
    chapter = max(nums) if nums else 0
    fs_rows = db.list_foreshadow(conn)
    out = {
        "root": os.path.abspath(args.root),
        "chapters": len(nums),
        "entities": [dict(r) for r in conn.execute(
            "SELECT kind, COUNT(*) AS n FROM entities GROUP BY kind ORDER BY kind").fetchall()],
        "events": int(conn.execute("SELECT COUNT(*) c FROM events").fetchone()["c"]),
        "foreshadowing": {
            "total": len(fs_rows),
            "open": len([f for f in fs_rows if f["status"] not in fs_mod.TERMINAL]),
            "overdue": [f["code"] for f in fs_mod.overdue(fs_rows, chapter)],
            "leaking": [f["code"] for f in fs_rows if f["status"] == "STALE_LEAK"],
        },
        "threads": {t["status"]: t["code"] for t in db.list_threads(conn)},
        "world_rules": [dict(r) for r in conn.execute(
            "SELECT code,text,immutable FROM world_rules ORDER BY code").fetchall()],
        "snapshots": int(conn.execute("SELECT COUNT(*) c FROM snapshots").fetchone()["c"]),
        "warnings_unresolved": int(conn.execute(
            "SELECT COUNT(*) c FROM warnings WHERE resolved=0").fetchone()["c"]),
        "max_chapter": chapter,
    }
    conn.close()
    if args.json:
        jprint(out)
        return 0
    print("Canon 概览：%s" % out["root"])
    print("  章节        : %d（最高 ch-%04d）" % (out["chapters"], chapter))
    print("  实体        : %s" % ", ".join("%s=%d" % (e["kind"], e["n"]) for e in out["entities"]))
    print("  事件        : %d" % out["events"])
    print("  伏笔        : 共 %d，未回收 %d，逾期 %s，泄露待确认 %s"
          % (out["foreshadowing"]["total"], out["foreshadowing"]["open"],
             out["foreshadowing"]["overdue"] or "无", out["foreshadowing"]["leaking"] or "无"))
    print("  线程状态    : %s" % out["threads"])
    print("  世界规则    : %d 条" % len(out["world_rules"]))
    print("  快照        : %d" % out["snapshots"])
    print("  未解决警告  : %d" % out["warnings_unresolved"])
    return 0


def cmd_character(args) -> int:
    conn = _require(args.root)
    name = args.name
    state = db.get_character_state(conn, name)
    if not state:
        conn.close()
        raise NarrativeError("Canon 中没有人物 %r" % name)
    history = db.character_state_history(conn, name, limit=args.limit)
    rels = db.list_relationships(conn, state["name"])
    knows = [dict(r) for r in conn.execute(
        "SELECT fact_code,fact_text,learned_chapter,secret FROM knowledge "
        "WHERE knower_name=? ORDER BY fact_code", (state["name"],)).fetchall()]
    items = [i for i in db.list_items(conn) if i.get("holder_name") == state["name"]]
    events = []
    for r in conn.execute("SELECT * FROM events ORDER BY chapter").fetchall():
        if state["name"] in db._dec(r["participants"], []):
            events.append({"chapter": r["chapter"], "code": r["code"], "summary": r["summary"]})
    conn.close()
    out = {"character": state, "history": history, "relationships": rels,
           "knowledge": knows, "items": items, "events": events[-20:]}
    if args.json:
        jprint(out)
        return 0
    print("人物：%s" % state["name"])
    print("  位置=%s 目标=%s 情绪=%s 健康=%s 状态=%s"
          % (state.get("location"), state.get("goal"), state.get("emotion"),
             state.get("health"), state.get("status")))
    print("  信念=%s" % json.dumps(state.get("belief") or [], ensure_ascii=False))
    print("  持有=%s" % "、".join(i["name"] for i in items) or "  持有：-")
    print("  关系：")
    for r in rels:
        print("    %s → %s [%s/%s]" % (r["from"], r["to"], r["kind"], r["status"]))
    print("  知道的事实：")
    for k in knows:
        print("    %s%s %s" % (k["fact_code"], "(机密)" if k["secret"] else "",
                               (k["fact_text"] or "")[:40]))
    print("  状态变化历史（最近 %d 条）：" % len(history))
    for h in history:
        print("    ch-%s %s: %s → %s (%s)"
              % (h["chapter"], h["field"], json.dumps(h["before"], ensure_ascii=False),
                 json.dumps(h["after"], ensure_ascii=False), h["source"]))
    return 0


def cmd_event(args) -> int:
    conn = _require(args.root)
    code = args.code
    row = db.get_event(conn, code)
    if row is None:
        rows = conn.execute("SELECT * FROM events WHERE CAST(id AS TEXT)=? OR code=?",
                            (code, code)).fetchall()
        row = rows[0] if rows else None
    conn.close()
    if row is None:
        raise NarrativeError("没有事件 %r" % code)
    data = dict(row)
    for f in ("participants", "causes", "effects", "data"):
        data[f] = db._dec(data[f], [] if f != "data" else {})
    if args.json:
        jprint(data)
        return 0
    print("事件 %s（第%s章）" % (data.get("code"), data.get("chapter")))
    print("  类型：%s｜不可逆：%s" % (data.get("kind"), bool(data.get("irreversible"))))
    print("  摘要：%s" % data.get("summary"))
    print("  参与：%s｜地点：%s｜时间：%s"
          % ("、".join(data.get("participants") or []), data.get("location"),
             data.get("timeline_at")))
    print("  原因：%s｜结果：%s" % (data.get("causes"), data.get("effects")))
    return 0


def cmd_foreshadow(args) -> int:
    conn = _require(args.root)
    nums = chapter_numbers(args.root)
    chapter = args.at_chapter or (max(nums) if nums else 0)
    rows = db.list_foreshadow(conn, [args.status] if args.status else None)
    if args.code:
        rows = [r for r in rows if r["code"] == args.code]
    conn.close()
    out = [dict(fs_mod.lifecycle_status(r, chapter),
                title=r["title"], description=r["description"]) for r in rows]
    if args.json:
        jprint(out)
        return 0
    if not out:
        print("没有匹配的伏笔。")
        return 0
    for f in out:
        print("%s《%s》%s%s" % (f["code"], f["title"], f["status"],
                               "【逾期】" if f["overdue"] else ""))
        print("    计划回收 %s-%s｜阶段=%s｜泄露=%s"
              % (f["planned_payoff_start"], f["planned_payoff_end"], f["phase"],
                 f["leak_detected"]))
        if f["description"]:
            print("    %s" % f["description"])
    return 0


def cmd_thread(args) -> int:
    conn = _require(args.root)
    rows = db.list_threads(conn)
    if args.code:
        rows = [t for t in rows if t["code"] == args.code]
    conn.close()
    if args.json:
        jprint(rows)
        return 0
    for t in rows:
        print("%s《%s》%s 状态=%s 开启=%s 关闭=%s 目标=%s"
              % (t["code"], t["name"], t["kind"], t["status"], t["opened_chapter"],
                 t["closed_chapter"], t["goal"]))
    return 0


def cmd_knowledge(args) -> int:
    conn = _require(args.root)
    sql = "SELECT * FROM knowledge"
    params = []
    if args.fact:
        sql += " WHERE fact_code=?"
        params.append(args.fact)
    sql += " ORDER BY fact_code, knower_name"
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    conn.close()
    if args.json:
        jprint(rows)
        return 0
    for r in rows:
        print("%s → %s%s：%s（ch-%s, %s）"
              % (r["fact_code"], r["knower_name"], "(机密)" if r["secret"] else "",
                 (r["fact_text"] or "")[:40], r["learned_chapter"], r["source"]))
    if not rows:
        print("（无知识记录）")
    return 0


def cmd_item(args) -> int:
    conn = _require(args.root)
    rows = db.list_items(conn)
    if args.code:
        rows = [i for i in rows if i["code"] == args.code]
    conn.close()
    if args.json:
        jprint(rows)
        return 0
    for i in rows:
        print("%s《%s》持有=%s 位置=%s 状态=%s"
              % (i["code"], i["name"], i.get("holder_name") or "-", i["location"] or "-",
                 i["state"] or "-"))
    if not rows:
        print("（无物品记录）")
    return 0


def cmd_list(args) -> int:
    conn = _require(args.root)
    if args.kind:
        rows = [dict(r) for r in conn.execute(
            "SELECT id,kind,name,status,first_chapter,last_chapter FROM entities "
            "WHERE kind=? ORDER BY name", (args.kind,)).fetchall()]
    else:
        rows = [dict(r) for r in conn.execute(
            "SELECT id,kind,name,status,first_chapter,last_chapter FROM entities "
            "ORDER BY kind,name").fetchall()]
    conn.close()
    if args.json:
        jprint(rows)
        return 0
    for r in rows:
        print("%-12s %-20s %-8s ch-%s..%s" % (r["kind"], r["name"], r["status"],
                                              r["first_chapter"], r["last_chapter"]))
    return 0


def cmd_state(args) -> int:
    """Reconstruct the world at a chapter by replaying snapshots into a temp DB."""
    root = args.root
    _require(root).close()
    import tempfile
    tmp = os.path.join(tempfile.mkdtemp(prefix="canon-state-"), "replay.db")
    info = db.replay(root, int(args.at_chapter), tmp)
    conn = db.connect(root, tmp)
    states = db.all_character_states(conn)
    fs_rows = db.list_foreshadow(conn)
    threads = db.list_threads(conn)
    items = db.list_items(conn)
    events = [dict(r) for r in conn.execute(
        "SELECT code,chapter,kind,summary FROM events ORDER BY chapter, id").fetchall()]
    timeline = db.timeline_all(conn)
    volumes = db.volumes_all(conn)
    conn.close()
    os.remove(tmp)
    out = {"at_chapter": args.at_chapter, "replayed": info,
           "characters": states, "foreshadowing": [dict(r) for r in fs_rows],
           "threads": threads, "items": items, "events": events, "timeline": timeline,
           "volumes": volumes}
    if args.json:
        jprint(out)
        return 0
    print("第 %s 章时的世界状态（由 %d 个快照重建）" % (args.at_chapter, info["snapshots_applied"]))
    print("  人物：")
    for s in states:
        print("    %s @%s 目标=%s 状态=%s" % (s["name"], s.get("location"), s.get("goal"),
                                              s.get("status")))
    print("  伏笔：")
    for f in fs_rows:
        print("    %s %s" % (dict(f)["code"], dict(f)["status"]))
    print("  时间线末尾：%s" % (timeline[-1]["world_time"] if timeline else "-"))
    return 0


def cmd_snapshot(args) -> int:
    root = args.root
    _require(root).close()
    payload = db.load_snapshot(root, int(args.chapter))
    if payload is None:
        raise NarrativeError("ch-%04d 没有 snapshot" % args.chapter)
    if args.json:
        jprint(payload)
        return 0
    print("ch-%04d canon snapshot（%s）" % (payload["chapter"], payload.get("created_at")))
    print("  字数=%s 卷=%s 时间=%s" % (payload.get("chars"), payload.get("volume"),
                                       (payload.get("timeline") or {}).get("world_time")))
    print("  BEFORE 人物：%s" % json.dumps(payload.get("before", {}).get("characters", {}),
                                           ensure_ascii=False)[:400])
    print("  EVENTS：")
    for e in payload.get("events") or []:
        print("    %s [%s] %s" % (e.get("code"), e.get("kind"), e.get("summary")))
    print("  AFTER 人物：%s" % json.dumps(payload.get("after", {}).get("characters", {}),
                                          ensure_ascii=False)[:400])
    print("  状态变化：")
    for c in payload.get("character_changes") or []:
        print("    %s.%s %s → %s" % (c.get("character"), c.get("field"),
                                     json.dumps(c.get("before"), ensure_ascii=False),
                                     json.dumps(c.get("after"), ensure_ascii=False)))
    print("  伏笔变化：%s" % [(f.get("code"), f.get("before"), f.get("after"))
                              for f in payload.get("foreshadow_changes") or []])
    print("  知识新增：%s" % [(k.get("knower"), k.get("fact_code"))
                              for k in payload.get("knowledge_added") or []])
    print("  警告：%d 条" % len(payload.get("warnings") or []))
    return 0


def cmd_compare(args) -> int:
    root = args.root
    _require(root).close()
    a = db.load_snapshot(root, int(args.first))
    b = db.load_snapshot(root, int(args.second))
    if a is None or b is None:
        raise NarrativeError("快照缺失：%s / %s" % (args.first, args.second))
    diff = db.diff_snapshots(a, b)
    if args.json:
        jprint(diff)
        return 0
    for k, v in diff.items():
        print("%-24s %s" % (k, v))
    return 0


def cmd_history(args) -> int:
    conn = _require(args.root)
    sql = "SELECT * FROM canon_changes WHERE 1=1"
    params = []
    if args.kind:
        sql += " AND entity_kind=?"
        params.append(args.kind)
    if args.ref:
        sql += " AND entity_ref=?"
        params.append(args.ref)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(args.limit)
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    conn.close()
    if args.json:
        jprint(rows)
        return 0
    for r in rows:
        print("[%s] %s %s %s.%s %s → %s%s"
              % (r["ts"], r["actor"], r["action"], r["entity_kind"], r["field"] or "-",
                 (r["before_value"] or "-")[:40], (r["after_value"] or "-")[:40],
                 "  //%s" % r["reason"] if r["reason"] else ""))
    if not rows:
        print("（无变更记录）")
    return 0


def cmd_changes(args) -> int:
    return cmd_history(args)


def cmd_warnings(args) -> int:
    conn = _require(args.root)
    sql = "SELECT * FROM warnings"
    if not args.all:
        sql += " WHERE resolved=0"
    sql += " ORDER BY id DESC LIMIT ?"
    rows = [dict(r) for r in conn.execute(sql, (args.limit,)).fetchall()]
    conn.close()
    if args.json:
        jprint(rows)
        return 0
    for r in rows:
        print("[ch-%s][%s/%s] %s" % (r["chapter"], r["code"], r["severity"], r["message"]))
    if not rows:
        print("（无未解决警告）")
    return 0


# ================================================================ update

UPDATE_TARGETS = ("character", "rule", "foreshadow", "thread", "knowledge", "item",
                  "relationship", "alias", "rename")


def cmd_update(args) -> int:
    """Human canon correction. Everything is logged with who/when/what/why."""
    conn = _require(args.root)
    nums = chapter_numbers(args.root)
    chapter = args.chapter if args.chapter is not None else (max(nums) if nums else 0)
    value = args.value
    if args.json_value:
        try:
            value = json.loads(args.json_value)
        except ValueError:
            conn.close()
            raise NarrativeError("--json-value 不是合法 JSON：%r" % args.json_value)

    patch = {"characters": {}, "character_changes": [], "foreshadowing": {},
             "threads": {}, "world_rules": [], "knowledge_added": [], "items": [],
             "relationships": [], "aliases": {}, "renames": {}, "entities": []}
    action = "manual_update"
    entity_kind, entity_ref, field, before = args.kind, args.ref, args.field, None

    if args.kind == "character":
        state = db.get_character_state(conn, args.ref)
        if not state:
            conn.close()
            raise NarrativeError("Canon 中没有人物 %r（先用 canon.py import-md 或 extract 建人）"
                                 % args.ref)
        if args.field not in db.CHARACTER_FIELDS:
            conn.close()
            raise NarrativeError("字段 %r 非法（合法：%s）"
                                 % (args.field, "|".join(db.CHARACTER_FIELDS)))
        before = state.get(args.field)
        patch["characters"][args.ref] = {args.field: value}
    elif args.kind == "rule":
        patch["world_rules"].append({"code": args.ref, "text": value if isinstance(value, str)
                                     else json.dumps(value, ensure_ascii=False),
                                     "allow_rewrite": True})
    elif args.kind == "foreshadow":
        row = db.get_foreshadow(conn, args.ref)
        if row is None:
            conn.close()
            raise NarrativeError("没有伏笔 %r" % args.ref)
        before = row[args.field] if args.field in row.keys() else None
        patch["foreshadowing"][args.ref] = {args.field: value}
    elif args.kind == "thread":
        patch["threads"][args.ref] = {args.field: value}
    elif args.kind == "knowledge":
        patch["knowledge_added"].append({"knower": args.actor, "fact_code": args.ref,
                                         "fact_text": value if isinstance(value, str)
                                         else json.dumps(value, ensure_ascii=False),
                                         "source": "human"})
    elif args.kind == "item":
        patch["items"].append({"code": args.ref, args.field or "state": value})
    elif args.kind == "relationship":
        patch["relationships"].append({"from": args.ref, "to": args.target,
                                       "kind": args.field or "related", "status": value})
    elif args.kind == "alias":
        patch["aliases"][args.ref] = value if isinstance(value, list) else [value]
        action = "manual_alias"
    elif args.kind == "rename":
        patch["renames"][args.ref] = value
        action = "manual_rename"
    else:
        conn.close()
        raise NarrativeError("--kind 必须是 %s" % "|".join(UPDATE_TARGETS))

    if not args.reason:
        conn.close()
        raise NarrativeError("人工修改 Canon 必须提供 --reason（谁/何时/何因都要可追踪）")

    conn.execute("BEGIN IMMEDIATE")
    db._apply_manual_patch(conn, patch, chapter, "manual")
    commit_id = db.new_id("MAN")
    path = db.write_manual_patch(root=args.root, patch=patch, chapter=chapter,
                                 actor=args.actor, reason=args.reason, commit_id=commit_id)
    payload = {"schema": 1, "kind": "manual", "chapter": chapter, "commit_id": commit_id,
               "created_at": now_iso(), "actor": args.actor, "reason": args.reason,
               "patch": patch}
    sid = db.insert_manual_snapshot_row(conn, path, payload)
    db.log_change(conn, action, actor=args.actor, entity_kind=entity_kind,
                  entity_ref=entity_ref, field=field, before=before, after=value,
                  reason=args.reason, source="human", chapter=chapter, commit_id=commit_id)
    conn.execute("COMMIT")
    res = _resync(args.root, conn)
    conn.close()
    jprint({"ok": True, "kind": args.kind, "ref": args.ref, "field": args.field,
            "before": before, "after": value, "actor": args.actor, "reason": args.reason,
            "snapshot": path, "snapshot_id": sid, "derived_files": len(res["written"]),
            "note": "人工修改已写入 Canon 并记录 canon_changes（可 rollback）"})
    return 0


# ================================================================ ddl / schema / verify

def cmd_ddl(args) -> int:
    text = db.ddl()
    if args.out:
        atomic_write_text(args.out, text)
        jprint({"ok": True, "out": os.path.abspath(args.out), "bytes": len(text),
                "tables": text.count("CREATE TABLE")})
    else:
        sys.stdout.write(text)
    return 0


def cmd_schema(args) -> int:
    conn = _require(args.root)
    report = db.schema_report(conn)
    conn.close()
    if args.json:
        jprint(report)
        return 0
    for t in report:
        print("表 %s" % t["table"])
        print("    字段：%s" % ", ".join(
            "%s %s%s" % (c["name"], c["type"] or "TEXT", " PK" if c["pk"] else "")
            for c in t["columns"]))
        for i in t["indexes"]:
            print("    索引：%s%s(%s)" % ("UNIQUE " if i["unique"] else "", i["name"],
                                          ", ".join(i["columns"])))
    return 0


def cmd_verify(args) -> int:
    _require(args.root).close()
    report = db.verify_replay(args.root)
    if args.json:
        jprint(report)
    else:
        print("快照回放校验：%s" % ("PASS" if report["ok"] else "FAIL"))
        print("  回放章节数：%s｜快照数：%s｜base：%s"
              % (report["chapters_replayed"], report["snapshots"], report["base_used"]))
        for d in report["differences"]:
            print("  差异表 %s：live=%s replay=%s"
                  % (d["table"], d["live_rows"], d["replay_rows"]))
    return 0 if report["ok"] else 1


def cmd_import_md(args) -> int:
    conn = _require(args.root)
    result = import_bible_md(args.root, conn, actor=args.actor)
    conn.close()
    jprint(result)
    return 0 if result.get("ok") else 2


def cmd_rollback(args) -> int:
    import novel_state as ns
    return ns.cmd_rollback(argparse.Namespace(
        root=args.root, to_chapter=args.to_chapter, chapter=None,
        reason=args.reason or "canon rollback", actor=args.actor))


# ================================================================ parser

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", required=True, help="novel project root")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("show", help="canon overview")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_show)

    c = sub.add_parser("character", help="character state + history")
    c.add_argument("name")
    c.add_argument("--limit", type=int, default=30)
    c.add_argument("--json", action="store_true")
    c.set_defaults(func=cmd_character)

    e = sub.add_parser("event", help="show one event")
    e.add_argument("code")
    e.add_argument("--json", action="store_true")
    e.set_defaults(func=cmd_event)

    f = sub.add_parser("foreshadow", help="foreshadowing lifecycle")
    f.add_argument("--code", default="")
    f.add_argument("--status", default="")
    f.add_argument("--at-chapter", type=int, default=0)
    f.add_argument("--json", action="store_true")
    f.set_defaults(func=cmd_foreshadow)

    t = sub.add_parser("thread", help="plot threads")
    t.add_argument("--code", default="")
    t.add_argument("--json", action="store_true")
    t.set_defaults(func=cmd_thread)

    k = sub.add_parser("knowledge", help="who knows what")
    k.add_argument("--fact", default="")
    k.add_argument("--json", action="store_true")
    k.set_defaults(func=cmd_knowledge)

    it = sub.add_parser("item", help="item custody")
    it.add_argument("--code", default="")
    it.add_argument("--json", action="store_true")
    it.set_defaults(func=cmd_item)

    l = sub.add_parser("list", help="list entities")
    l.add_argument("--kind", default="", choices=("", "character", "location",
                                                  "organization", "item"))
    l.add_argument("--json", action="store_true")
    l.set_defaults(func=cmd_list)

    st = sub.add_parser("state", help="reconstruct the world at a chapter")
    st.add_argument("--at-chapter", type=int, required=True)
    st.add_argument("--json", action="store_true")
    st.set_defaults(func=cmd_state)

    sn = sub.add_parser("snapshot", help="show a chapter canon snapshot")
    sn.add_argument("--chapter", type=int, required=True)
    sn.add_argument("--json", action="store_true")
    sn.set_defaults(func=cmd_snapshot)

    cp = sub.add_parser("compare", help="diff two chapter snapshots")
    cp.add_argument("first", type=int)
    cp.add_argument("second", type=int)
    cp.add_argument("--json", action="store_true")
    cp.set_defaults(func=cmd_compare)

    h = sub.add_parser("history", help="canon change log")
    h.add_argument("--kind", default="")
    h.add_argument("--ref", default="")
    h.add_argument("--limit", type=int, default=40)
    h.add_argument("--json", action="store_true")
    h.set_defaults(func=cmd_history)

    ch = sub.add_parser("changes", help="alias of history")
    ch.add_argument("--kind", default="")
    ch.add_argument("--ref", default="")
    ch.add_argument("--limit", type=int, default=40)
    ch.add_argument("--json", action="store_true")
    ch.set_defaults(func=cmd_changes)

    w = sub.add_parser("warnings", help="extractor/audit warnings")
    w.add_argument("--all", action="store_true")
    w.add_argument("--limit", type=int, default=50)
    w.add_argument("--json", action="store_true")
    w.set_defaults(func=cmd_warnings)

    u = sub.add_parser("update", help="HUMAN canon correction (who/when/what/why)")
    u.add_argument("--kind", required=True, choices=UPDATE_TARGETS)
    u.add_argument("--ref", required=True, help="entity name / code")
    u.add_argument("--field", default="", help="field to change")
    u.add_argument("--value", default=None, help="new value (string)")
    u.add_argument("--json-value", default="", help="new value as JSON (lists/objects)")
    u.add_argument("--target", default="", help="relationship target")
    u.add_argument("--chapter", type=int, default=None)
    u.add_argument("--actor", default="human")
    u.add_argument("--reason", required=True)
    u.set_defaults(func=cmd_update)

    d = sub.add_parser("ddl", help="print/write the canonical DDL")
    d.add_argument("--out", default="")
    d.set_defaults(func=cmd_ddl)

    sc = sub.add_parser("schema", help="table/column/index inventory")
    sc.add_argument("--json", action="store_true")
    sc.set_defaults(func=cmd_schema)

    v = sub.add_parser("verify-snapshots", help="prove the snapshot log reproduces canon.db")
    v.add_argument("--json", action="store_true")
    v.set_defaults(func=cmd_verify)

    im = sub.add_parser("import-md", help="bible.md -> Canon")
    im.add_argument("--actor", default="human")
    im.set_defaults(func=cmd_import_md)

    rb = sub.add_parser("rollback", help="roll Canon back to a chapter")
    rb.add_argument("--to-chapter", type=int, required=True)
    rb.add_argument("--reason", default="")
    rb.add_argument("--actor", default="human")
    rb.set_defaults(func=cmd_rollback)

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
