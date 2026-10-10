#!/usr/bin/env python3
"""Full state validation: prove the project is internally consistent.

Answers, mechanically, the questions a 1,000-chapter project cannot answer by
inspection:

  * does progress.json agree with the chapter files?
  * are chapter numbers contiguous, and does every file hash match what was committed?
  * does every chapter have a snapshot, a plan, a passing audit and a DB row?
  * are there orphan entities, dangling event participants, relationships to
    non-existent characters, knowledge for non-existent knowers?
  * do foreshadowing records reference chapters that exist?
  * are thread statuses legal? is the timeline monotonic?
  * is a round open with unfinished tasks? is a transaction half-open?

Every finding carries a stable code, a severity and the exact entity involved, so
the report is machine-checkable (exit code 1 on any `error`).
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import canon_db as db  # noqa: E402
import foreshadow as fs_mod  # noqa: E402
import tx as tx_mod  # noqa: E402
from _common import (  # noqa: E402
    NarrativeError,
    canonical_text,
    chapter_numbers,
    chapter_path,
    jprint,
    load_progress,
    paths,
    read_text,
    sha256_text,
)

ERROR = "error"
WARN = "warning"
INFO = "info"


def _f(code, message, severity=ERROR, **extra):
    d = {"code": code, "message": message, "severity": severity}
    d.update(extra)
    return d


def validate(root: str) -> dict:
    findings = []
    p = paths(root)
    prog = load_progress(root)
    nums = chapter_numbers(root)
    max_ch = max(nums) if nums else 0

    # ---- 1. chapter files vs progress / contiguity
    if nums and nums != list(range(1, max_ch + 1)):
        missing = [n for n in range(1, max_ch + 1) if n not in set(nums)]
        findings.append(_f("CHAPTER_GAP", "章节号不连续，缺失：%s" % missing,
                           ERROR, missing=missing))
    committed = int(prog.get("chapters_committed", -1))
    if committed != len(nums):
        findings.append(_f("PROGRESS_CHAPTER_MISMATCH",
                           "progress.json 记录 chapters_committed=%s，磁盘实际 %d 章"
                           % (committed, len(nums)), ERROR,
                           expected=len(nums), actual=committed))

    # ---- 2. canon DB presence
    if not db.db_exists(root):
        findings.append(_f("CANON_DB_MISSING",
                           "state/canon.db 不存在：Canon 记忆未初始化（运行 init 或 "
                           "novel_state.py migrate）", ERROR))
        return _summarize(findings, root, nums)
    conn = db.connect(root)

    # ---- 3. chapters table vs files vs snapshots vs audits
    rows = {r["number"]: r for r in db.chapters_all(conn)}
    for n in nums:
        row = rows.get(n)
        path = chapter_path(root, n)
        if row is None:
            findings.append(_f("CHAPTER_ROW_MISSING",
                               "ch-%04d 有正文文件但 Canon 中无章节记录（半提交）" % n,
                               ERROR, chapter=n))
            continue
        text = read_text(path)
        actual = sha256_text(canonical_text(text))
        if row["sha256"] and row["sha256"] != actual:
            findings.append(_f("CHAPTER_HASH_MISMATCH",
                               "ch-%04d 正文在提交后被修改（Canon 记录的哈希与磁盘不一致）" % n,
                               ERROR, chapter=n, expected=row["sha256"], actual=actual))
        if db.load_snapshot(root, n) is None:
            findings.append(_f("SNAPSHOT_MISSING", "ch-%04d 缺少 canon snapshot" % n,
                               ERROR, chapter=n))
        snap_row = conn.execute("SELECT id FROM snapshots WHERE chapter=? AND kind='delta'",
                                (n,)).fetchone()
        if snap_row is None:
            findings.append(_f("SNAPSHOT_ROW_MISSING",
                               "ch-%04d 的 snapshot 未登记到 Canon（snapshots 表无行）" % n,
                               ERROR, chapter=n))
        audit = db.audit_for(conn, n)
        imported = (row["audit_status"] == "imported")
        if imported:
            findings.append(_f("IMPORTED_CHAPTER",
                               "ch-%04d 为导入章节，没有原生审计记录（如需门禁请运行 extract/audit）" % n,
                               INFO, chapter=n))
        elif audit is None:
            findings.append(_f("AUDIT_MISSING", "ch-%04d 从未被审计" % n, ERROR, chapter=n))
        elif not audit["pass"]:
            findings.append(_f("AUDIT_FAILED", "ch-%04d 最新审计未通过" % n, ERROR, chapter=n))
        elif audit["sha256"] and audit["sha256"] != actual:
            findings.append(_f("AUDIT_STALE",
                               "ch-%04d 的审计记录对应的是另一份文本（审计后又被修改）" % n,
                               ERROR, chapter=n))
        if db.get_plan(conn, n, "chapter") is None:
            findings.append(_f("PLAN_MISSING", "ch-%04d 没有 chapter plan" % n,
                               WARN, chapter=n))
        gates = db.get_gates(conn, n)
        for g in ("plan", "audit", "canon"):
            if gates.get(g) in ("missing", "fail"):
                findings.append(_f("GATE_NOT_PASSED",
                                   "ch-%04d 的 %s 门状态为 %s" % (n, g, gates.get(g)),
                                   WARN if gates.get(g) == "missing" else ERROR,
                                   chapter=n, gate=g))

    for n in sorted(set(rows) - set(nums)):
        findings.append(_f("CHAPTER_ORPHAN_ROW",
                           "Canon 中存在 ch-%04d 的章节记录，但磁盘没有正文文件（需 rollback）" % n,
                           ERROR, chapter=n))
    for n in sorted(set(rows) - set(nums)):
        if db.load_snapshot(root, n) is not None:
            findings.append(_f("SNAPSHOT_ORPHAN",
                               "ch-%04d 的 snapshot 存在但没有正文（需 rollback 清理）" % n,
                               WARN, chapter=n))

    # ---- 4. entity integrity
    characters = {r["name"] for r in conn.execute(
        "SELECT name FROM entities WHERE kind='character'").fetchall()}
    locations = {r["name"] for r in conn.execute(
        "SELECT name FROM entities WHERE kind='location'").fetchall()}
    all_entity_ids = {r["id"] for r in conn.execute("SELECT id FROM entities").fetchall()}

    for r in conn.execute(
            "SELECT e.name FROM entities e LEFT JOIN character_states s ON s.entity_id=e.id "
            "WHERE e.kind='character' AND s.entity_id IS NULL").fetchall():
        findings.append(_f("ORPHAN_CHARACTER",
                           "人物 %s 没有任何状态记录" % r["name"], WARN, entity=r["name"]))

    for r in conn.execute("SELECT * FROM character_states").fetchall():
        if r["entity_id"] not in all_entity_ids:
            findings.append(_f("DANGLING_STATE_REF",
                               "character_states 引用了不存在的人物 id=%s" % r["entity_id"],
                               ERROR, entity=str(r["entity_id"])))
        if r["location"] and r["location"] not in locations:
            findings.append(_f("UNKNOWN_LOCATION_REF",
                               "人物状态的位置 %r 不在 Canon location 中" % r["location"],
                               WARN, entity=str(r["entity_id"])))

    # ---- 5. events
    for r in conn.execute("SELECT * FROM events").fetchall():
        parts = db._dec(r["participants"], [])
        for name in parts:
            if name not in characters:
                findings.append(_f("EVENT_UNKNOWN_PARTICIPANT",
                                   "事件 %s 引用了不存在的人物 %s" % (r["code"] or r["id"], name),
                                   ERROR, entity=name, chapter=r["chapter"]))
        if r["chapter"] not in set(nums):
            findings.append(_f("EVENT_CHAPTER_MISSING",
                               "事件 %s 指向不存在的章节 %s" % (r["code"] or r["id"], r["chapter"]),
                               ERROR, chapter=r["chapter"]))
        for cause in db._dec(r["causes"], []):
            other = db.get_event(conn, cause)
            if other is None:
                findings.append(_f("DANGLING_CAUSE",
                                   "事件 %s 的 cause=%s 不存在" % (r["code"] or r["id"], cause),
                                   ERROR, chapter=r["chapter"]))

    # ---- 6. relationships
    for r in conn.execute(
            "SELECT r.id, a.name fa, b.name fb FROM relationships r "
            "LEFT JOIN entities a ON a.id=r.from_entity "
            "LEFT JOIN entities b ON b.id=r.to_entity").fetchall():
        if r["fa"] is None or r["fb"] is None:
            findings.append(_f("RELATIONSHIP_DANGLING_REF",
                               "关系 id=%s 引用了不存在的人物" % r["id"], ERROR))
        else:
            if r["fa"] not in characters:
                findings.append(_f("RELATIONSHIP_UNKNOWN_CHARACTER",
                                   "关系引用了非人物实体 %s" % r["fa"], ERROR, entity=r["fa"]))
            if r["fb"] not in characters:
                findings.append(_f("RELATIONSHIP_UNKNOWN_CHARACTER",
                                   "关系引用了非人物实体 %s" % r["fb"], ERROR, entity=r["fb"]))

    # ---- 7. knowledge
    for r in conn.execute("SELECT * FROM knowledge").fetchall():
        if r["knower_name"] and r["knower_name"] not in characters:
            findings.append(_f("KNOWLEDGE_UNKNOWN_KNOWER",
                               "知识 %s 的知情人 %s 不存在" % (r["fact_code"], r["knower_name"]),
                               ERROR, entity=r["knower_name"]))
        if r["learned_chapter"] is not None and int(r["learned_chapter"]) > max_ch:
            findings.append(_f("KNOWLEDGE_FUTURE_CHAPTER",
                               "知识 %s 的获知章节 %s 超过已写章节 %s"
                               % (r["fact_code"], r["learned_chapter"], max_ch),
                               ERROR, chapter=r["learned_chapter"]))

    # ---- 8. foreshadowing
    for raw in conn.execute("SELECT * FROM foreshadowings").fetchall():
        r = dict(raw)
        if r["status"] not in fs_mod.FORESHADOW_STATES:
            findings.append(_f("ILLEGAL_FORESHADOW_STATUS",
                               "伏笔 %s 的状态 %r 非法" % (r["code"], r["status"]), ERROR,
                               entity=r["code"]))
        for field in ("planted_chapter", "resolved_chapter"):
            val = r[field]
            if val is not None and max_ch and int(val) > max_ch:
                findings.append(_f("FORESHADOW_UNKNOWN_CHAPTER",
                                   "伏笔 %s 的 %s=%s 指向不存在的章节" % (r["code"], field, val),
                                   ERROR, entity=r["code"]))
        start, end = r["planned_payoff_start"], r["planned_payoff_end"]
        if start is not None and end is not None and int(end) < int(start):
            findings.append(_f("FORESHADOW_WINDOW_INVERTED",
                               "伏笔 %s 的计划回收区间倒置 %s-%s" % (r["code"], start, end),
                               ERROR, entity=r["code"]))
        if start is None and r["status"] not in fs_mod.TERMINAL:
            findings.append(_f("FORESHADOW_NO_WINDOW",
                               "伏笔 %s 没有计划回收区间，无法判定逾期" % r["code"],
                               WARN, entity=r["code"]))
        if r["status"] == "RESOLVED" and r["resolved_chapter"] is None:
            findings.append(_f("FORESHADOW_RESOLVED_NO_CHAPTER",
                               "伏笔 %s 标记为 RESOLVED 但没有回收章节" % r["code"],
                               ERROR, entity=r["code"]))
        info = fs_mod.lifecycle_status(r, max_ch)
        if info["overdue"]:
            findings.append(_f("FORESHADOW_OVERDUE",
                               "伏笔 %s 已逾期未回收（计划 %s-%s）"
                               % (r["code"], start, end), WARN, entity=r["code"]))
    for r in db.list_foreshadow(conn):
        if r["status"] == "STALE_LEAK":
            findings.append(_f("FORESHADOW_LEAKED",
                               "伏笔 %s 被标记为提前泄露，需要人工确认" % r["code"],
                               WARN, entity=r["code"]))

    # ---- 9. threads
    for r in conn.execute("SELECT * FROM plot_threads").fetchall():
        if r["status"] not in db.THREAD_STATES:
            findings.append(_f("ILLEGAL_THREAD_STATUS",
                               "线程 %s 的状态 %r 非法（合法：%s）"
                               % (r["code"], r["status"], "|".join(db.THREAD_STATES)),
                               ERROR, entity=r["code"]))
        if r["opened_chapter"] is not None and max_ch and int(r["opened_chapter"]) > max_ch:
            findings.append(_f("THREAD_UNKNOWN_CHAPTER",
                               "线程 %s 的开启章节 %s 不存在" % (r["code"], r["opened_chapter"]),
                               ERROR, entity=r["code"]))
        if r["status"] == "resolved" and r["closed_chapter"] is None:
            findings.append(_f("THREAD_RESOLVED_NO_CHAPTER",
                               "线程 %s 已 resolved 但没有关闭章节" % r["code"], WARN,
                               entity=r["code"]))

    # ---- 10. timeline
    tl = db.timeline_all(conn)
    for i in range(1, len(tl)):
        if str(tl[i]["world_time"]) < str(tl[i - 1]["world_time"]):
            findings.append(_f("TIMELINE_REGRESSION",
                               "时间线倒退：第%s章 %s < 第%s章 %s"
                               % (tl[i]["chapter"], tl[i]["world_time"],
                                  tl[i - 1]["chapter"], tl[i - 1]["world_time"]),
                               ERROR, chapter=tl[i]["chapter"]))
    for t in tl:
        if t["chapter"] not in set(nums):
            findings.append(_f("TIMELINE_ORPHAN_CHAPTER",
                               "时间线记录了不存在的章节 %s" % t["chapter"], WARN,
                               chapter=t["chapter"]))

    # ---- 11. items
    for i in db.list_items(conn):
        if i.get("holder_id") and i.get("holder_id") not in all_entity_ids:
            findings.append(_f("ITEM_DANGLING_HOLDER",
                               "物品 %s 的持有者 id=%s 不存在" % (i["code"], i["holder_id"]),
                               ERROR, entity=i["code"]))
        if i.get("location") and i["location"] not in locations:
            findings.append(_f("ITEM_UNKNOWN_LOCATION",
                               "物品 %s 的位置 %r 不在 Canon 中" % (i["code"], i["location"]),
                               WARN, entity=i["code"]))

    # ---- 12. rounds and transactions
    open_rounds = [dict(r) for r in conn.execute(
        "SELECT * FROM round_runs WHERE status='open'").fetchall()]
    for r in open_rounds:
        planned = db._dec(r["planned_chapters"], [])
        done = {t["chapter"] for t in db.round_tasks(conn, r["round_index"])
                if t["stage"] == "committed" and t["status"] == "done"}
        pending = [c for c in planned if c not in done]
        # An open round mid-flight is the normal working state, so this is a
        # warning here. Blocking is round-done's job, not validation's.
        findings.append(_f("ROUND_OPEN_WITH_TASKS",
                           "第 %s 轮仍处于 open 状态，未完成任务：%s"
                           % (r["round_index"], pending or "（全部已完成，等待 round-done）"),
                           WARN, round=r["round_index"], pending=pending))
    pending_tx = tx_mod.pending_work(root, conn)["open_transactions"]
    for t in pending_tx:
        findings.append(_f("TRANSACTION_HALF_OPEN",
                           "存在半开事务 %s（chapter=%s state=%s）：运行 novel_state.py recover"
                           % (t["id"], t["chapter"], t["state"]), ERROR))
    if tx_mod.pending_work(root, conn).get("open_db_transactions"):
        for t in tx_mod.pending_work(root, conn)["open_db_transactions"]:
            findings.append(_f("TRANSACTION_DB_ROW_OPEN",
                               "transactions 表存在未完成记录 %s" % t.get("id"), WARN))

    # ---- 13. drafts / staging leftovers
    work = tx_mod.pending_work(root, conn)
    for d in work["uncommitted_drafts"]:
        findings.append(_f("UNCOMMITTED_DRAFT",
                           "存在未提交草稿 ch-%04d（%s）" % (d["chapter"], d["path"]),
                           INFO, chapter=d["chapter"]))
    for dr in work["plans"]:
        if not dr["committed"]:
            findings.append(_f("PLAN_WITHOUT_CHAPTER",
                               "存在没有对应章节的计划 ch-%04d" % dr["chapter"],
                               INFO, chapter=dr["chapter"]))

    # ---- 14. duplicate / inconsistent derived state
    for name in ("warnings.jsonl", "journal.jsonl"):
        fp = os.path.join(p["state"], name)
        if os.path.isfile(fp) and os.path.getsize(fp) == 0:
            findings.append(_f("EMPTY_LOG", "%s 为空文件" % name, INFO))

    conn.close()
    return _summarize(findings, root, nums)


def _summarize(findings, root, nums) -> dict:
    errors = [f for f in findings if f["severity"] == ERROR]
    warns = [f for f in findings if f["severity"] == WARN]
    infos = [f for f in findings if f["severity"] == INFO]
    by_code = {}
    for f in findings:
        by_code[f["code"]] = by_code.get(f["code"], 0) + 1
    return {
        "root": os.path.abspath(root),
        "chapters": len(nums),
        "ok": not errors,
        "errors": len(errors), "warnings": len(warns), "infos": len(infos),
        "by_code": by_code,
        "findings": findings,
    }


def render(report: dict) -> str:
    lines = ["# 状态校验报告",
             "项目：%s" % report["root"],
             "章节：%d" % report["chapters"],
             "结果：%s（错误 %d / 警告 %d / 提示 %d）"
             % ("PASS" if report["ok"] else "FAIL", report["errors"],
                report["warnings"], report["infos"]), ""]
    if not report["findings"]:
        lines.append("未发现问题。")
        return "\n".join(lines)
    for sev, label in ((ERROR, "错误"), (WARN, "警告"), (INFO, "提示")):
        subset = [f for f in report["findings"] if f["severity"] == sev]
        if not subset:
            continue
        lines.append("## %s（%d）" % (label, len(subset)))
        for f in subset:
            extra = ""
            for k in ("chapter", "entity", "gate", "round"):
                if f.get(k) is not None:
                    extra += " %s=%s" % (k, f[k])
            lines.append("- [%s]%s %s" % (f["code"], extra, f["message"]))
        lines.append("")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Validate the whole project state")
    ap.add_argument("--root", required=True)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--strict", action="store_true",
                    help="exit 1 on any error (default when errors exist)")
    ap.add_argument("--report", default="", help="also write the report to this path")
    ap.add_argument("--warn-as-error", action="store_true")
    args = ap.parse_args(argv)

    try:
        report = validate(args.root)
    except NarrativeError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    if args.report:
        from _common import atomic_write_text
        atomic_write_text(args.report, render(report))
    if args.json:
        jprint(report)
    else:
        print(render(report))
    if args.warn_as_error and (report["errors"] or report["warnings"]):
        return 1
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
