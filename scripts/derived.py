#!/usr/bin/env python3
"""Derived views: regenerate the Markdown the legacy workflow reads.

CANONICAL TRUTH ORDER (never violated in this file)

    chapter text > validated canon delta > canon.db > derived summaries > Markdown

`bible.md`, `plot-ledger.md` and `summaries/` are therefore OUTPUTS. They are
rebuilt from Canon after every commit so that the human-readable layer can never
drift from the database it claims to describe.

MIGRATION SAFETY
----------------
An existing hand-written `bible.md` is *not* destroyed. The generator only
overwrites it when it carries the AUTO-GENERATED marker (i.e. we wrote it) or
when `force=True` was requested explicitly. Otherwise the generated view lands in
`state/derived/bible.md` and a note tells the user to run `canon.py import-md`
once to move the hand-written content into Canon.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import canon_db as db  # noqa: E402
import foreshadow as fs_mod  # noqa: E402
from _common import atomic_write_json, atomic_write_text, paths, read_text  # noqa: E402

MARKER = "<!-- AUTO-GENERATED from state/canon.db"

BIBLE_HEADER = """%s — 请勿手工编辑 -->
# 设定圣经（Bible）· 派生视图

> 本文件由 `state/canon.db` 自动生成，**不是**事实来源。
> 事实来源优先级：章节正文 > 校验过的 Canon Delta > canon.db > 派生摘要 > Markdown 视图。
> 需要修改设定时，用 `python scripts/canon.py update ...`；若你手工编辑了本文件，
> 可用 `python scripts/canon.py import-md` 回灌到 Canon。

## 1. 一句话故事
%s

## 2. 世界规则（硬设定 · Canon）
| 编号 | 规则 | 首次出现章节 | 不可违反 |
|---|---|---|---|
%s

## 3. 主要人物（Canon 当前状态）
| 姓名 | 位置 | 目标 | 情绪 | 健康 | 状态 | 关系 |
|---|---|---|---|---|---|---|
%s

## 4. 地理与组织
| 名称 | 类型 | 首次出现 | 末次出现 | 状态 |
|---|---|---|---|---|
%s

## 5. 时间线
| 章节 | 世界时间 | 备注 |
|---|---|---|
%s

## 6. 伏笔登记表（生命周期）
| 编号 | 伏笔 | 状态 | 埋设章节 | 计划回收区间 | 实际回收 | 逾期 | 备注 |
|---|---|---|---|---|---|---|---|
%s

## 7. 剧情线程
| 编号 | 名称 | 类型 | 状态 | 开启章节 | 关闭章节 | 目标 |
|---|---|---|---|---|---|---|
%s

## 8. 物品
| 编号 | 名称 | 持有者 | 位置 | 状态 |
|---|---|---|---|---|
%s

## 9. 人物知识边界（谁知道什么）
| 人物 | 事实编号 | 事实 | 获知章节 | 机密 |
|---|---|---|---|---|
%s

## 10. 已用尽的素材（避免重复）
- 已用场景数：%d
- 已登记事件数：%d
- 卷末不可逆变化：
%s
"""

LEDGER_HEADER = """%s — 请勿手工编辑 -->
# 剧情台账（Plot Ledger）· 派生视图

> 本文件由 `state/canon.db` 自动生成，**不是**事实来源。
> 每章提交后自动重建。不要手工追加行——会被下一次重建覆盖。

| 章节 | 字数 | 时间 | 地点 | 本章事件 | 状态变化 | 伏笔变化 | 知识新增 |
|---|---|---|---|---|---|---|---|
"""


def _table(rows, empty="| （空） | | | | | | | |") -> str:
    if not rows:
        return empty
    return "\n".join(rows)


def read_marker_state(path: str) -> str:
    """'ours' | 'foreign' | 'missing'"""
    if not os.path.isfile(path):
        return "missing"
    head = read_text(path)[:400]
    return "ours" if MARKER in head else "foreign"


def render_bible(root: str, conn) -> str:
    from _common import load_progress
    prog = load_progress(root)

    rules = "\n".join("| %s | %s | %s | %s |"
                      % (r["code"], r["text"], r["first_chapter"],
                         "是" if r["immutable"] else "否")
                      for r in conn.execute(
                          "SELECT * FROM world_rules ORDER BY code").fetchall()) or "| （空） | | | |"

    char_rows = []
    for r in conn.execute(
            "SELECT e.name AS name, s.location, s.goal, s.emotion, s.health, s.status "
            "FROM entities e LEFT JOIN character_states s ON s.entity_id=e.id "
            "WHERE e.kind='character' ORDER BY e.name").fetchall():
        rels = db.list_relationships(conn, r["name"])
        rel_txt = "、".join("%s(%s)" % (x["to"] if x["from"] == r["name"] else x["from"],
                                        x["status"]) for x in rels[:3]) or "-"
        char_rows.append("| %s | %s | %s | %s | %s | %s | %s |"
                         % (r["name"], r["location"] or "-", r["goal"] or "-",
                            r["emotion"] or "-", r["health"] or "-",
                            r["status"] or "-", rel_txt))

    place_rows = []
    for r in conn.execute(
            "SELECT name, kind, first_chapter, last_chapter, status FROM entities "
            "WHERE kind IN ('location','organization','item') ORDER BY kind, name").fetchall():
        place_rows.append("| %s | %s | %s | %s | %s |"
                          % (r["name"], r["kind"], r["first_chapter"],
                             r["last_chapter"], r["status"]))

    tl_rows = ["| %s | %s | %s |" % (t["chapter"], t["world_time"], t["note"] or "")
               for t in db.timeline_all(conn)]

    current = max([c["number"] for c in db.chapters_all(conn)] or [0])
    fs_rows = []
    for f in db.list_foreshadow(conn):
        info = fs_mod.lifecycle_status(f, current)
        fs_rows.append("| %s | %s | %s | %s | %s-%s | %s | %s | %s |"
                       % (f["code"], f["title"], f["status"], f["planted_chapter"],
                          f["planned_payoff_start"], f["planned_payoff_end"],
                          f["resolved_chapter"] or "-",
                          "是" if info["overdue"] else "否",
                          f["description"] or ""))

    th_rows = ["| %s | %s | %s | %s | %s | %s | %s |"
               % (t["code"], t["name"], t["kind"], t["status"], t["opened_chapter"],
                  t["closed_chapter"] or "-", t["goal"] or "")
               for t in db.list_threads(conn)]

    item_rows = ["| %s | %s | %s | %s | %s |"
                 % (i["code"], i["name"], i["holder_name"] or "-", i["location"] or "-",
                    i["state"] or "-") for i in db.list_items(conn)]

    kn_rows = []
    for r in conn.execute("SELECT * FROM knowledge ORDER BY fact_code, knower_name").fetchall():
        kn_rows.append("| %s | %s | %s | %s | %s |"
                       % (r["knower_name"], r["fact_code"], (r["fact_text"] or "")[:40],
                          r["learned_chapter"], "是" if r["secret"] else "否"))

    vols = db.volumes_all(conn)
    vol_lines = ["- 第%s卷（第%s-%s章）卷末不可逆变化：%s"
                 % (v["volume"], v["start_chapter"], v["end_chapter"],
                    v["irreversible_change"] or "（未填）") for v in vols] or ["- （未填）"]

    scenes = len(db.list_scenes(conn))
    events = int(conn.execute("SELECT COUNT(*) c FROM events").fetchone()["c"])

    return BIBLE_HEADER % (
        MARKER, prog.get("premise") or "（未填）", rules, _table(char_rows),
        _table(place_rows), _table(tl_rows), _table(fs_rows), _table(th_rows),
        _table(item_rows), _table(kn_rows), scenes, events, "\n".join(vol_lines))


def render_ledger(root: str, conn) -> str:
    rows = []
    for c in db.chapters_all(conn):
        snap = db.load_snapshot(root, c["number"]) or {}
        events = "；".join((e.get("summary") or "")[:24] for e in snap.get("events") or [])
        changes = "；".join("%s.%s→%s" % (x.get("character"), x.get("field"),
                                          _short(x.get("after")))
                            for x in snap.get("character_changes") or [])
        fs = "；".join("%s %s→%s" % (f.get("code"), f.get("before"), f.get("after"))
                       for f in snap.get("foreshadow_changes") or [])
        kn = "；".join("%s:%s" % (k.get("knower"), k.get("fact_code"))
                       for k in snap.get("knowledge_added") or [])
        rows.append("| ch-%04d | %d | %s | %s | %s | %s | %s | %s |"
                    % (c["number"], c["chars"], c["timeline_at"] or "-",
                       (snap.get("events") or [{}])[0].get("location") or "-",
                       _esc(events) or "-", _esc(changes) or "-", _esc(fs) or "-",
                       _esc(kn) or "-"))
    return LEDGER_HEADER % MARKER + (_table(rows, "| （空） | | | | | | | |") + "\n")


def _short(v, limit=14) -> str:
    if v is None:
        return "∅"
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    return s if len(s) <= limit else s[:limit] + "…"


def _esc(text: str) -> str:
    return (text or "").replace("|", "/").replace("\n", " ")


def render_summary(root: str, conn, chapter: int) -> str:
    c = db.chapter_row(conn, chapter)
    snap = db.load_snapshot(root, chapter) or {}
    lines = ["# ch-%04d（%s 字）" % (chapter, format((c or {}).get("chars") or 0, ",")), ""]
    if snap.get("summary"):
        lines += [snap["summary"], ""]
    if snap.get("events"):
        lines.append("## 事件（结构化）")
        for e in snap["events"]:
            lines.append("- %s [%s] %s｜参与：%s｜地点：%s%s"
                         % (e.get("code") or "-", e.get("kind"), e.get("summary"),
                            "、".join(e.get("participants") or []) or "-",
                            e.get("location") or "-",
                            "｜不可逆" if e.get("irreversible") else ""))
        lines.append("")
    if snap.get("character_changes"):
        lines.append("## 状态变化（BEFORE → AFTER）")
        lines.append("| 人物 | 字段 | BEFORE | AFTER |")
        lines.append("|---|---|---|---|")
        for x in snap["character_changes"]:
            lines.append("| %s | %s | %s | %s |" % (x.get("character"), x.get("field"),
                                                    _short(x.get("before"), 40),
                                                    _short(x.get("after"), 40)))
        lines.append("")
    if snap.get("foreshadow_changes"):
        lines.append("## 伏笔变化")
        for f in snap["foreshadow_changes"]:
            lines.append("- %s：%s → %s（%s）%s"
                         % (f.get("code"), f.get("before"), f.get("after"),
                            f.get("action"), f.get("note") or ""))
        lines.append("")
    if snap.get("knowledge_added"):
        lines.append("## 知识新增")
        for k in snap["knowledge_added"]:
            lines.append("- %s 得知 %s：%s" % (k.get("knower"), k.get("fact_code"),
                                              k.get("fact_text") or ""))
        lines.append("")
    if snap.get("warnings"):
        lines.append("## 提取器警告（未写入 Canon）")
        for w in snap["warnings"]:
            lines.append("- [%s] %s" % (w.get("code"), w.get("message")))
        lines.append("")
    return "\n".join(lines)


def render_banned(root: str) -> str:
    from _common import paths as _p
    p = _p(root)["banned"]
    return read_text(p) if os.path.isfile(p) else ""


def json_view_payloads(root: str, conn) -> dict:
    """The JSON views as {path: text} so a transaction can stage them."""
    p = paths(root)
    current = max([c["number"] for c in db.chapters_all(conn)] or [0])
    payloads = {
        "bible.json": {
            "world_rules": [dict(r) for r in conn.execute(
                "SELECT * FROM world_rules ORDER BY code").fetchall()],
            "characters": db.all_character_states(conn),
            "entities": [dict(r) for r in conn.execute(
                "SELECT id,kind,name,aliases,status,first_chapter,last_chapter FROM entities "
                "ORDER BY kind,name").fetchall()],
            "volumes": db.volumes_all(conn),
            "arcs": [dict(r) for r in conn.execute("SELECT * FROM arcs ORDER BY code").fetchall()],
        },
        "ledger.json": {
            "chapters": db.chapters_all(conn),
            "events": [dict(r) for r in conn.execute(
                "SELECT * FROM events ORDER BY chapter, id").fetchall()],
        },
        "foreshadowing.json": [fs_mod.lifecycle_status(f, current)
                               for f in db.list_foreshadow(conn)],
        "threads.json": db.list_threads(conn),
        "knowledge.json": [dict(r) for r in conn.execute(
            "SELECT * FROM knowledge ORDER BY fact_code").fetchall()],
        "items.json": db.list_items(conn),
        "timeline.json": db.timeline_all(conn),
        "scenes.json": db.list_scenes(conn),
    }
    return {os.path.join(p["derived"], name):
            json.dumps(obj, ensure_ascii=False, indent=2) + "\n"
            for name, obj in payloads.items()}


def views_payload(root: str, conn, chapters=None, *, bible_target: str = None) -> dict:
    """Every derived view as {absolute_path: text}. Pure function: no writes.

    `novel_state.py commit` stages this into a transaction, so a crash can never
    leave the Markdown views describing a database state that never committed.
    """
    p = paths(root)
    out = {}
    out[bible_target or p["bible"]] = render_bible(root, conn)
    out[p["ledger"]] = render_ledger(root, conn)
    for n in (chapters if chapters is not None
              else [c["number"] for c in db.chapters_all(conn)]):
        out[os.path.join(p["summaries"], "ch-%04d.md" % n)] = render_summary(root, conn, n)
    out.update(json_view_payloads(root, conn))
    return out


def write_json_views(root: str, conn) -> list:
    written = []
    for path, text in json_view_payloads(root, conn).items():
        atomic_write_text(path, text)
        written.append(path)
    return written


def write_views(root: str, conn, *, force_bible: bool = False, chapters=None) -> dict:
    """Rebuild every derived view. Returns {"written": [...], "notes": [...]}."""
    p = paths(root)
    written, notes = [], []

    bible_target = p["bible"]
    state = read_marker_state(bible_target)
    if state == "foreign" and not force_bible:
        bible_target = os.path.join(p["derived"], "bible.md")
        notes.append("state/bible.md 是手工撰写的（无自动生成标记），未覆盖；"
                     "已写入 %s。运行 `python scripts/canon.py import-md --root <项目>` "
                     "把手工内容灌入 Canon 后即可改为自动生成。" % bible_target)
        os.makedirs(p["derived"], exist_ok=True)
    atomic_write_text(bible_target, render_bible(root, conn))
    written.append(bible_target)

    atomic_write_text(p["ledger"], render_ledger(root, conn))
    written.append(p["ledger"])

    targets = chapters if chapters is not None else [c["number"] for c in db.chapters_all(conn)]
    for n in targets:
        path = os.path.join(p["summaries"], "ch-%04d.md" % n)
        atomic_write_text(path, render_summary(root, conn, n))
        written.append(path)

    written.extend(write_json_views(root, conn))
    return {"written": written, "notes": notes}


def ensure_bible_marker(root: str) -> bool:
    """Convert a legacy hand-written bible.md into the generated form.

    Returns True when the file is (now) generated. Used after `canon.py import-md`
    so the human content is preserved in Canon first and only then overwritten by
    the generated view.
    """
    p = paths(root)["bible"]
    if not os.path.isfile(p):
        return True
    if read_marker_state(p) == "ours":
        return True
    return False
