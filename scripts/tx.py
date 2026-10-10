#!/usr/bin/env python3
"""Filesystem + SQLite transaction for one chapter commit, and crash recovery.

THE PROBLEM
-----------
The pre-upgrade `commit` did four independent things in sequence:

    shutil.copyfile(draft, chapters/ch-NNNN.txt)
    append_text(plot-ledger.md, row)
    write_text(summaries/ch-NNNN.md, ...)
    save_progress(...)

Any failure in the middle left a half-committed chapter: the text on disk, the
ledger missing a row, progress.json disagreeing with reality. Nobody could tell
afterwards, and `next` had no way to find out. There was no journal, no staging,
no rollback.

THE DESIGN
----------
    PREPARE   write every file this commit will own into state/tx/<id>/ (staging),
              back up any file that already exists, write the journal.
    DB        one SQLite transaction: chapter row, canon delta, snapshot rows,
              gates, traceability. If it raises, nothing is committed.
    PUBLISH   os.replace() each staged file into place (atomic on one volume).
              The journal already lists every target, so it can be redone.
    FINISH    mark the journal committed and drop the staging directory.

`recover()` is idempotent and complete: an interrupted commit is either finished
(if the database committed) or discarded (if it did not). `next` runs it
automatically, so a crashed process never requires a human to inspect a dozen
files.
"""
from __future__ import annotations

import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _common import (  # noqa: E402
    NarrativeError,
    append_jsonl,
    atomic_write_json,
    atomic_write_text,
    chapter_path,
    chapter_numbers,
    load_json,
    now_iso,
    paths,
    read_text,
    sha256_text,
)

STATES = ("prepared", "db_committed", "published", "committed", "rolled_back")


def _safe_name(name: str) -> str:
    """Windows forbids ':' in filenames; staging names are diagnostic only."""
    out = "".join(ch if (ch.isalnum() or ch in "._-") else "_" for ch in str(name))
    return out[:80] or "item"


def tx_dir(root: str, tx_id: str) -> str:
    return os.path.join(paths(root)["tx"], tx_id)


def journal_path(root: str, tx_id: str) -> str:
    return os.path.join(tx_dir(root, tx_id), "journal.json")


class CommitTransaction:
    """Stages every artifact of a chapter commit, then publishes atomically.

    Usage:
        tx = CommitTransaction(root, chapter)
        tx.write_text("chapter", chapter_path(root, n), text)
        tx.write_text("ledger", paths(root)["ledger"], new_ledger)
        tx.publish()            # after the DB transaction committed
        tx.finish()
    """

    def __init__(self, root: str, chapter: int, tx_id: str = None):
        self.root = root
        self.chapter = int(chapter)
        self.id = tx_id or ("TX-%s-ch%04d" % (now_iso().replace(":", "").replace("-", ""),
                                              self.chapter))
        self.dir = tx_dir(root, self.id)
        os.makedirs(self.dir, exist_ok=True)
        self.files = []
        self.notes = []
        self._journal = {
            "id": self.id,
            "chapter": self.chapter,
            "state": "prepared",
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "files": self.files,
            "notes": self.notes,
        }
        self._flush()

    # ------------------------------------------------------------ staging
    def _flush(self) -> None:
        self._journal["updated_at"] = now_iso()
        atomic_write_json(journal_path(self.root, self.id), self._journal)

    def write_text(self, name: str, final_path: str, content: str, *,
                   binary_src: str = None) -> dict:
        """Stage one artifact. `binary_src` copies a file instead of text."""
        safe = _safe_name(name)
        stored = os.path.join(self.dir, "%02d-%s" % (len(self.files), safe))
        if binary_src is not None:
            shutil.copyfile(binary_src, stored)
        else:
            atomic_write_text(stored, content)
        entry = {
            "name": name,
            "final": os.path.abspath(final_path),
            "staged": stored,
            "backup": None,
            "published": False,
            "sha256": sha256_text(read_text(stored)) if binary_src is None
            else None,
        }
        if os.path.isfile(final_path):
            backup = os.path.join(self.dir, "%02d-%s.prev" % (len(self.files), safe))
            try:
                shutil.copyfile(final_path, backup)
                entry["backup"] = backup
            except OSError:
                entry["backup"] = None
        self.files.append(entry)
        self._flush()
        return entry

    def note(self, text: str) -> None:
        self.notes.append({"ts": now_iso(), "text": text})
        self._flush()

    # ------------------------------------------------------------ publish
    def publish(self) -> dict:
        for entry in self.files:
            if entry["published"]:
                continue
            target = entry["final"]
            os.makedirs(os.path.dirname(target), exist_ok=True)
            os.replace(entry["staged"], target)
            entry["published"] = True
            self._journal["state"] = "published"
            self._flush()
        return {"published": len([f for f in self.files if f["published"]])}

    def mark_db_committed(self) -> None:
        self._journal["state"] = "db_committed"
        self._flush()

    def finish(self) -> None:
        self._journal["state"] = "committed"
        self._flush()
        append_jsonl(paths(self.root)["journal"],
                     {"ts": now_iso(), "tx": self.id, "chapter": self.chapter,
                      "state": "committed", "files": len(self.files)})
        shutil.rmtree(self.dir, ignore_errors=True)

    # ------------------------------------------------------------ rollback
    def rollback_files(self) -> dict:
        """Undo publication: restore backups, delete files that did not exist."""
        restored, deleted = [], []
        for entry in reversed(self.files):
            if not entry["published"]:
                continue
            if entry["backup"] and os.path.isfile(entry["backup"]):
                os.replace(entry["backup"], entry["final"])
                restored.append(entry["final"])
            elif os.path.isfile(entry["final"]):
                os.remove(entry["final"])
                deleted.append(entry["final"])
        self._journal["state"] = "rolled_back"
        self._flush()
        shutil.rmtree(self.dir, ignore_errors=True)
        return {"restored": restored, "deleted": deleted}


# ================================================================ recovery

def _load_journals(root: str) -> list:
    d = paths(root)["tx"]
    if not os.path.isdir(d):
        return []
    out = []
    for name in sorted(os.listdir(d)):
        jp = os.path.join(d, name, "journal.json")
        data = load_json(jp, None)
        if isinstance(data, dict):
            data["_dir"] = os.path.join(d, name)
            out.append(data)
    return out


def recover(root: str, conn=None, *, apply: bool = True) -> dict:
    """Finish or discard every interrupted commit. Idempotent.

    Returns {"found": [...], "actions": [...], "clean": bool}.
    """
    found, actions = [], []
    for j in _load_journals(root):
        state = j.get("state") or "prepared"
        entry = {"id": j.get("id"), "chapter": j.get("chapter"), "state": state,
                 "files": len(j.get("files") or [])}
        found.append(entry)
        if state == "prepared":
            # the database never committed: nothing to publish, drop the staging
            action = "discard_staging"
            if apply:
                shutil.rmtree(j["_dir"], ignore_errors=True)
                if conn is not None:
                    try:
                        conn.execute("UPDATE transactions SET state='rolled_back', "
                                     "finished_at=? WHERE id=?", (now_iso(), j.get("id")))
                    except Exception:
                        pass
            actions.append({"tx": j.get("id"), "action": action,
                            "why": "数据库事务未提交，丢弃 staging，章节保持未提交状态"})
            continue
        if state in ("db_committed", "published"):
            # the database DID commit: complete publication, never lose the chapter
            done = []
            if apply:
                for f in j.get("files") or []:
                    if f.get("published"):
                        continue
                    staged, final = f.get("staged"), f.get("final")
                    if staged and os.path.isfile(staged) and final:
                        os.makedirs(os.path.dirname(final), exist_ok=True)
                        os.replace(staged, final)
                        f["published"] = True
                        done.append(final)
                j["state"] = "committed"
                atomic_write_json(os.path.join(j["_dir"], "journal.json"), j)
                if conn is not None:
                    try:
                        conn.execute("UPDATE transactions SET state='committed', "
                                     "finished_at=? WHERE id=?", (now_iso(), j.get("id")))
                    except Exception:
                        pass
                shutil.rmtree(j["_dir"], ignore_errors=True)
            actions.append({"tx": j.get("id"), "action": "complete_publish",
                            "why": "数据库已提交，补齐文件发布",
                            "files": done})
            continue
        if state in ("committed", "rolled_back"):
            if apply:
                shutil.rmtree(j["_dir"], ignore_errors=True)
            actions.append({"tx": j.get("id"), "action": "cleanup", "why": "事务已完成"})
    return {"found": found, "actions": actions, "clean": not found}


def pending_work(root: str, conn=None) -> dict:
    """The Recovery Manager: what is this project's actual state right now?

    Answers, without a human reading a dozen files:
      * did the previous chapter commit?
      * is there an uncommitted draft / staging file?
      * is there a plan without a committed chapter?
      * is there a failing audit?
      * is there a half-open transaction?
      * is a round still open?
    """
    p = paths(root)
    nums = set(chapter_numbers(root))
    drafts = []
    d = p["drafts"]
    if os.path.isdir(d):
        import re as _re
        for name in sorted(os.listdir(d)):
            m = _re.fullmatch(r"ch-(\d{4,})\.md", name)
            if m:
                n = int(m.group(1))
                path = os.path.join(d, name)
                drafts.append({"chapter": n, "path": path,
                               "chars": len(read_text(path).strip()),
                               "committed": n in nums})
    plans = []
    pd = p["plans"]
    if os.path.isdir(pd):
        import re as _re
        for name in sorted(os.listdir(pd)):
            m = _re.fullmatch(r"ch-(\d{4,})\.plan\.json", name)
            if m:
                n = int(m.group(1))
                plans.append({"chapter": n, "committed": n in nums,
                              "path": os.path.join(pd, name)})
    parts = []
    parts_dir = p["parts"]
    if os.path.isdir(parts_dir):
        for name in sorted(os.listdir(parts_dir)):
            parts.append(os.path.join(parts_dir, name))
    audits = []
    ad = p["audits"]
    if os.path.isdir(ad):
        audits = [os.path.join(ad, n) for n in sorted(os.listdir(ad))]

    open_tx = _load_journals(root)
    open_round = None
    open_db_tx = []
    failing_audit = []
    if conn is not None:
        try:
            open_round = __import__("canon_db").latest_open_round(conn)
            open_db_tx = __import__("canon_db").open_transactions(conn)
            rows = conn.execute("SELECT chapter, pass FROM audits ORDER BY id DESC").fetchall()
            seen = set()
            for r in rows:
                if r["chapter"] in seen:
                    continue
                seen.add(r["chapter"])
                if not r["pass"] and r["chapter"] in nums:
                    failing_audit.append(r["chapter"])
        except Exception:
            pass

    resume = "继续正常流程"
    if open_tx:
        resume = "存在未完成事务：先运行 novel_state.py recover"
    elif any(not dr["committed"] for dr in drafts) and drafts:
        uncommitted = [dr["chapter"] for dr in drafts if not dr["committed"]]
        resume = "存在未提交草稿 ch-%s：继续写并 commit，或丢弃草稿" % ", ".join(
            "%04d" % n for n in uncommitted[:5])
    elif failing_audit:
        resume = "存在未通过审计的已提交章节 %s：先修复" % failing_audit[:5]
    elif open_round:
        resume = "第 %s 轮尚未收轮：运行 round-done" % open_round.get("round_index")

    return {
        "committed_chapters": sorted(nums),
        "next_chapter": (max(nums) + 1) if nums else 1,
        "drafts": drafts,
        "uncommitted_drafts": [d0 for d0 in drafts if not d0["committed"]],
        "plans": plans,
        "part_files": parts,
        "audit_files": audits,
        "open_transactions": [{"id": t.get("id"), "chapter": t.get("chapter"),
                               "state": t.get("state")} for t in open_tx],
        "open_db_transactions": open_db_tx,
        "open_round": open_round,
        "failing_audit_chapters": failing_audit,
        "resume": resume,
        "needs_recover": bool(open_tx),
    }
