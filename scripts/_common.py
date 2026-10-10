"""Shared helpers for the million-word-novel skill (Long-Context Narrative OS).

No third-party dependencies. Python 3.8+.

This module owns three things and nothing else:
  1. filesystem layout (`paths`),
  2. deterministic primitives (hashing, atomic writes, JSON/JSONL IO),
  3. length accounting (`count_chars`) and progress.json access.

Everything narrative lives in canon_db.py; everything workflow lives in novel_state.py.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import sys

# Windows consoles default to GBK; force UTF-8 so Chinese/JSON output never
# raises UnicodeEncodeError mid-run (which would abort a 100-round job).
for _stream_name in ("stdout", "stderr"):
    _s = getattr(sys, _stream_name, None)
    if _s is not None and hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

DEFAULT_TARGET = 1_000_000
DEFAULT_CHARS_PER_CHAPTER = 3000
DEFAULT_CHAPTERS_PER_ROUND = 5
DEFAULT_VOLUMES = 5
DEFAULT_COUNT_MODE = "no_ws"

COUNT_MODES = ("no_ws", "cjk", "all")

# progress.json schema written by this version. Schema 1 (the pre-upgrade repo)
# is migrated in place by load_progress(); no data is discarded.
PROGRESS_SCHEMA = 2

# A chapter commit is a filesystem transaction; every file it touches is listed
# here once so staging, publication and recovery cannot drift apart.
COMMIT_FILES = (
    "chapters", "ledger", "summary", "progress", "derived_bible",
    "derived_ledger", "derived_summary", "snapshot",
)

_CJK_RANGES = (
    (0x3400, 0x4DBF),   # CJK Ext A
    (0x4E00, 0x9FFF),   # CJK Unified
    (0xF900, 0xFAFF),   # CJK Compatibility
    (0x20000, 0x2A6DF),  # Ext B
)
_PUNCT = set("，。！？；：、“”‘’（）《》【】—…·〈〉「」『』～!?,.;:\"'()[]{}<>-—_\u3000 \t\r\n")


class NarrativeError(Exception):
    """Raised for user-facing failures that must produce exit code 2."""


def die(msg: str, code: int = 2):
    sys.stderr.write("error: %s\n" % msg)
    raise SystemExit(code)


def now_iso() -> str:
    return _dt.datetime.now().replace(microsecond=0).isoformat()


def today_iso() -> str:
    return _dt.date.today().isoformat()


# ---------------------------------------------------------------- paths

def paths(root: str) -> dict:
    """Every path the system may touch. Legacy keys are kept verbatim so that
    scripts written against the pre-upgrade layout keep working."""
    root = os.path.abspath(root)
    state = os.path.join(root, "state")

    # Legacy projects used `export/`; new projects use `exports/`. Whichever
    # already exists wins, so an old project is never split across two dirs.
    exports = os.path.join(root, "exports")
    legacy_export = os.path.join(root, "export")
    if os.path.isdir(legacy_export) and not os.path.isdir(exports):
        exports = legacy_export

    return {
        "root": root,
        "chapters": os.path.join(root, "chapters"),
        "drafts": os.path.join(root, "drafts"),
        "parts": os.path.join(root, "parts"),
        "state": state,
        "summaries": os.path.join(state, "summaries"),
        "snapshots": os.path.join(state, "snapshots"),
        "plans": os.path.join(state, "plans"),
        "audits": os.path.join(state, "audits"),
        "derived": os.path.join(state, "derived"),
        "tx": os.path.join(state, "tx"),
        "references": os.path.join(root, "references"),
        "tests": os.path.join(root, "tests"),
        "export": exports,
        "exports": exports,
        "legacy_export": legacy_export,
        "progress": os.path.join(state, "progress.json"),
        "canon": os.path.join(state, "canon.db"),
        "bible": os.path.join(state, "bible.md"),
        "outline": os.path.join(state, "outline.md"),
        "ledger": os.path.join(state, "plot-ledger.md"),
        "last_context": os.path.join(state, "last-context.md"),
        "round_brief": os.path.join(state, "round-brief.md"),
        "banned": os.path.join(state, "banned-phrases.txt"),
        "warnings": os.path.join(state, "warnings.jsonl"),
        "journal": os.path.join(state, "journal.jsonl"),
    }


def ensure_dirs(root: str) -> dict:
    p = paths(root)
    for key in ("chapters", "drafts", "parts", "state", "summaries", "snapshots",
                "plans", "audits", "derived", "tx", "export", "references", "tests"):
        os.makedirs(p[key], exist_ok=True)
    return p


def chapter_path(root: str, number: int) -> str:
    return os.path.join(paths(root)["chapters"], "ch-%04d.txt" % number)


def staging_chapter_path(root: str, number: int) -> str:
    return os.path.join(paths(root)["drafts"], "ch-%04d.md" % number)


def chapter_numbers(root: str) -> list:
    d = paths(root)["chapters"]
    if not os.path.isdir(d):
        return []
    out = []
    for name in os.listdir(d):
        m = re.fullmatch(r"ch-(\d{4,})\.txt", name)
        if m:
            out.append(int(m.group(1)))
    return sorted(out)


# ---------------------------------------------------------------- io primitives

def read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read()


def write_text(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def atomic_write_text(path: str, text: str) -> None:
    """Write via a sibling temp file + os.replace, so a crash never leaves a
    half-written state file behind (progress.json, snapshots, derived views)."""
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, ".%s.tmp" % os.path.basename(path))
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def append_text(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def load_json(path: str, default=None):
    if not os.path.isfile(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (ValueError, OSError):
        return default


def atomic_write_json(path: str, obj) -> None:
    atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=2) + "\n")


def append_jsonl(path: str, obj) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(obj, ensure_ascii=False) + "\n")


def read_jsonl(path: str) -> list:
    if not os.path.isfile(path):
        return []
    out = []
    for line in read_text(path).splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(65536), b""):
            h.update(block)
    return h.hexdigest()


def normalize_text(text: str) -> str:
    """Canonical form used for hashing prose: line endings are irrelevant, but
    nothing else is. Trailing whitespace on a line is not a canon change."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(ln.rstrip() for ln in text.split("\n"))


def canonical_text(text: str) -> str:
    """THE canonical prose form: the single representation that audit hashes,
    commit hashes and validation hashes all agree on.

    Without this, `audit --file draft` and `commit --file draft` could compute
    different digests for the same draft (trailing newline, CRLF, a stray space),
    which would make the audit gate reject text that had in fact been audited.
    """
    body = normalize_text(text)
    if not body.endswith("\n"):
        body += "\n"
    return body


def slug(text: str, fallback: str = "item") -> str:
    out = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "-", text or "").strip("-")
    return out[:48] or fallback


# ---------------------------------------------------------------- progress

def _migrate_progress(data: dict) -> dict:
    """Schema 1 -> 2. Purely additive: no legacy key is removed or renamed."""
    schema = int(data.get("schema") or 1)
    if schema >= PROGRESS_SCHEMA:
        return data
    data["schema"] = PROGRESS_SCHEMA
    data.setdefault("round_char_budget", 40000)
    data.setdefault("part_chars", 3500)
    data.setdefault("snapshot_base_interval", 25)
    data.setdefault("auto_next", False)
    data.setdefault("llm_provider", "rule")
    data.setdefault("gate_policy", {
        "require_plan": True,
        "require_audit": True,
        "require_canon_consistency": True,
    })
    data["migrated_from_schema"] = schema
    return data


def load_progress(root: str) -> dict:
    p = paths(root)["progress"]
    if not os.path.isfile(p):
        die("no progress.json under %s - run: novel_state.py init --root <dir>" % root)
    with open(p, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    return _migrate_progress(data)


def save_progress(root: str, data: dict) -> None:
    data["updated_at"] = now_iso()
    atomic_write_text(paths(root)["progress"],
                      json.dumps(data, ensure_ascii=False, indent=2) + "\n")


# ---------------------------------------------------------------- counting

def _is_cjk(ch: str) -> bool:
    cp = ord(ch)
    for lo, hi in _CJK_RANGES:
        if lo <= cp <= hi:
            return True
    return False


def count_chars(text: str, mode: str = DEFAULT_COUNT_MODE) -> int:
    if mode == "all":
        return len(text)
    stripped = "".join(ch for ch in text if not ch.isspace())
    if mode == "no_ws":
        return len(stripped)
    if mode == "cjk":
        return sum(1 for ch in stripped if _is_cjk(ch))
    die("unknown count mode: %s (use %s)" % (mode, "|".join(COUNT_MODES)))


def book_chars(root: str, mode: str = None) -> int:
    prog = load_progress(root)
    mode = mode or prog.get("count_mode", DEFAULT_COUNT_MODE)
    total = 0
    for n in chapter_numbers(root):
        total += count_chars(read_text(chapter_path(root, n)), mode)
    return total


def chapter_stats(root: str, number: int, mode: str) -> dict:
    p = chapter_path(root, number)
    if not os.path.isfile(p):
        return {"number": number, "exists": False, "chars": 0}
    text = read_text(p)
    return {"number": number, "exists": True, "chars": count_chars(text, mode)}


def tolerance(prog: dict) -> tuple:
    per_chapter = max(1, int(prog.get("chars_per_chapter", DEFAULT_CHARS_PER_CHAPTER)))
    lo, hi = prog.get("chars_per_chapter_tolerance",
                      [int(per_chapter * 0.8), int(per_chapter * 1.25)])
    return int(lo), int(hi)


# ---------------------------------------------------------------- volumes

def volume_bounds(prog: dict) -> list:
    """Volume boundaries.

    If progress.json carries an explicit `volume_plan` (a list of chapters per
    volume, e.g. [42, 50, 50, 50, 50]) that wins: it is the only way to describe
    a book whose early volumes are already written at a different density than
    the linear estimate would assume.
    """
    plan = prog.get("volume_plan")
    if plan:
        out = []
        start = 1
        for i, count in enumerate(plan, start=1):
            count = max(1, int(count))
            out.append({"volume": i, "start_chapter": start, "end_chapter": start + count - 1,
                        "chapters": count})
            start += count
        return out

    est = int(prog.get("estimated_chapters") or 0)
    vols = int(prog.get("volume_count") or 1)
    if est <= 0 or vols <= 0:
        return []
    out = []
    per = max(1, est // vols)
    start = 1
    for i in range(1, vols + 1):
        end = est if i == vols else start + per - 1
        out.append({"volume": i, "start_chapter": start, "end_chapter": end,
                    "chapters": end - start + 1})
        start = end + 1
    return out


def volume_of(prog: dict, number: int) -> int:
    bounds = volume_bounds(prog)
    for b in bounds:
        if b["start_chapter"] <= number <= b["end_chapter"]:
            return b["volume"]
    # past the last planned volume: keep extending the final one
    return bounds[-1]["volume"] if bounds else int(prog.get("volume_count") or 1)


# ---------------------------------------------------------------- outline (L2)

_VOL_HEAD = re.compile(r"^#{1,6}\s*第\s*([0-9一二三四五六七八九十]+)\s*卷")
_BULLET = re.compile(r"^[-*+]\s*([^:：]+)[:：]\s*(.*)$")


def _cn_num(token: str) -> int:
    digits = "一二三四五六七八九十"
    if token.isdigit():
        return int(token)
    if token == "十":
        return 10
    if token.startswith("十"):
        return 10 + digits.index(token[1]) + 1 if len(token) > 1 else 10
    if "十" in token:
        head, tail = token.split("十", 1)
        return digits.index(head) + 1 if len(tail) == 0 else (digits.index(head) + 1) * 10 + (
            digits.index(tail) + 1)
    return digits.index(token) + 1


def parse_outline(path: str) -> dict:
    """Parse outline.md deterministically into volume/arc records.

    outline.md is HUMAN-AUTHORED (a plan view, not a derived view). Parsing it is
    what finally makes it a first-class participant in every `next`: before this,
    the file existed but nothing ever read it.
    """
    if not os.path.isfile(path):
        return {"volumes": [], "arcs": [], "parsed": False, "issues": ["outline.md 不存在"]}
    text = read_text(path)
    volumes = []
    arcs = []
    issues = []
    cur = None
    for raw in text.split("\n"):
        line = raw.rstrip()
        m = _VOL_HEAD.match(line)
        if m:
            try:
                num = _cn_num(m.group(1))
            except ValueError:
                num = len(volumes) + 1
            cur = {"volume": num, "goal": "", "irreversible_change": "",
                   "main_conflict": "", "turns": "", "hook": "", "raw": [line]}
            volumes.append(cur)
            continue
        if cur is None:
            continue
        cur["raw"].append(line)
        b = _BULLET.match(line.strip())
        if not b:
            continue
        key, val = b.group(1).strip(), b.group(2).strip()
        if "卷末不可逆" in key or ("不可逆" in key and "卷目标" not in key and "目标" != key):
            cur["irreversible_change"] = val
        elif "卷目标" in key or key == "目标":
            cur["goal"] = val
            # legacy single-field form: `- 卷目标（不可逆变化）：X` means the volume
            # goal IS the irreversible change. An explicit 卷末不可逆变化 line,
            # if present, overrides this below.
            if "不可逆" in key and not cur.get("irreversible_change"):
                cur["irreversible_change"] = val
        elif "冲突" in key:
            cur["main_conflict"] = val
        elif "转折" in key:
            cur["turns"] = val
        elif "钩子" in key:
            cur["hook"] = val
        elif "arc" in key.lower() or "弧" in key:
            arcs.append({"code": val or ("A%d" % (len(arcs) + 1)), "name": val,
                         "volume": cur["volume"], "goal": ""})
    if not volumes:
        issues.append("outline.md 未解析出任何 `## 第N卷` 段落（L2 结构记忆将退化）")
    for v in volumes:
        if not v["irreversible_change"]:
            issues.append("第%d卷缺少「卷末不可逆变化」——百万字会退化为原地踏步" % v["volume"])
    for v in volumes:
        v["raw"] = "\n".join(v["raw"])
    return {"volumes": volumes, "arcs": arcs, "parsed": bool(volumes), "issues": issues}


def find_volume_record(outline: dict, volume: int) -> dict:
    for v in outline.get("volumes", []):
        if int(v["volume"]) == int(volume):
            return v
    return {}


# ---------------------------------------------------------------- ledger (compat view)

def ledger_rows(root: str) -> list:
    p = paths(root)["ledger"]
    if not os.path.isfile(p):
        return []
    return [ln for ln in read_text(p).splitlines() if ln.strip().startswith("|")]


def ledger_data_rows(root: str) -> list:
    rows = ledger_rows(root)
    out = []
    for ln in rows:
        cells = [c.strip() for c in ln.strip().strip("|").split("|")]
        if not cells or cells[0] in ("章节", "") or set(cells[0]) <= set("-: "):
            continue
        out.append(cells)
    return out


# ---------------------------------------------------------------- misc

def tail_chars(text: str, n: int) -> str:
    text = text.rstrip()
    return text[-n:] if len(text) > n else text


def tokenize(text: str) -> list:
    """Cheap deterministic tokenizer for similarity: CJK bigrams + latin words."""
    text = re.sub(r"\s+", "", text or "")
    grams = [text[i:i + 2] for i in range(len(text) - 1)]
    words = re.findall(r"[A-Za-z0-9_]{2,}", text)
    return grams + words


def jaccard(a, b) -> float:
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / float(len(sa | sb))


def status_dict(root: str) -> dict:
    prog = load_progress(root)
    mode = prog.get("count_mode", DEFAULT_COUNT_MODE)
    target = int(prog.get("target_chars", DEFAULT_TARGET))
    total = book_chars(root, mode)
    nums = chapter_numbers(root)
    committed = len(nums)
    per_round = max(1, int(prog.get("chapters_per_round", DEFAULT_CHAPTERS_PER_ROUND)))
    per_chapter = max(1, int(prog.get("chars_per_chapter", DEFAULT_CHARS_PER_CHAPTER)))
    remaining = max(0, target - total)
    rounds_left = (remaining + per_round * per_chapter - 1) // (per_round * per_chapter) if remaining else 0
    gaps = [n for n in range(1, (max(nums) + 1) if nums else 0) if n not in set(nums)]
    return {
        "title": prog.get("title"),
        "root": os.path.abspath(root),
        "schema": int(prog.get("schema") or 1),
        "count_mode": mode,
        "target_chars": target,
        "total_chars": total,
        "remaining_chars": remaining,
        "percent": round(total * 100.0 / target, 2) if target else 0.0,
        "chapters_committed": committed,
        "highest_chapter": max(nums) if nums else 0,
        "rounds_completed": int(prog.get("rounds_completed", 0)),
        "next_chapter": (max(nums) + 1) if nums else 1,
        "estimated_rounds_left": rounds_left,
        "complete": total >= target,
        "current_volume": volume_of(prog, (max(nums) + 1) if nums else 1),
        "volume_count": int(prog.get("volume_count", DEFAULT_VOLUMES)),
        "chapters_per_round": per_round,
        "chars_per_chapter": per_chapter,
        "chapter_gaps": gaps,
        "progress_chapters_committed": int(prog.get("chapters_committed", 0)),
        "canon_db_present": os.path.isfile(paths(root)["canon"]),
    }


def jprint(obj) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, indent=2) + "\n")
