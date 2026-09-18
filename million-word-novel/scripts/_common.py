"""Shared helpers for the million-word-novel skill.

No third-party dependencies. Python 3.8+.
"""
from __future__ import annotations

import datetime as _dt
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

_CJK_RANGES = (
    (0x3400, 0x4DBF),   # CJK Ext A
    (0x4E00, 0x9FFF),   # CJK Unified
    (0xF900, 0xFAFF),   # CJK Compatibility
    (0x20000, 0x2A6DF),  # Ext B
)
_PUNCT = set("，。！？；：、“”‘’（）《》【】—…·〈〉「」『』～!?,.;:\"'()[]{}<>-—_\u3000 \t\r\n")


def die(msg: str, code: int = 2):
    sys.stderr.write("error: %s\n" % msg)
    raise SystemExit(code)


def now_iso() -> str:
    return _dt.datetime.now().replace(microsecond=0).isoformat()


# ---------------------------------------------------------------- paths

def paths(root: str) -> dict:
    root = os.path.abspath(root)
    return {
        "root": root,
        "chapters": os.path.join(root, "chapters"),
        "state": os.path.join(root, "state"),
        "summaries": os.path.join(root, "state", "summaries"),
        "export": os.path.join(root, "export"),
        "progress": os.path.join(root, "state", "progress.json"),
        "bible": os.path.join(root, "state", "bible.md"),
        "outline": os.path.join(root, "state", "outline.md"),
        "ledger": os.path.join(root, "state", "plot-ledger.md"),
        "last_context": os.path.join(root, "state", "last-context.md"),
        "round_brief": os.path.join(root, "state", "round-brief.md"),
        "banned": os.path.join(root, "state", "banned-phrases.txt"),
    }


def chapter_path(root: str, number: int) -> str:
    return os.path.join(paths(root)["chapters"], "ch-%04d.txt" % number)


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


def read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read()


def write_text(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def append_text(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


# ---------------------------------------------------------------- progress

def load_progress(root: str) -> dict:
    p = paths(root)["progress"]
    if not os.path.isfile(p):
        die("no progress.json under %s - run: novel_state.py init --root <dir>" % root)
    with open(p, "r", encoding="utf-8") as fh:
        return json.load(fh)


def save_progress(root: str, data: dict) -> None:
    data["updated_at"] = now_iso()
    p = paths(root)["progress"]
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, p)


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
        out.append({"volume": i, "start_chapter": start, "end_chapter": end})
        start = end + 1
    return out


def volume_of(prog: dict, number: int) -> int:
    bounds = volume_bounds(prog)
    for b in bounds:
        if b["start_chapter"] <= number <= b["end_chapter"]:
            return b["volume"]
    # past the last planned volume: keep extending the final one
    return bounds[-1]["volume"] if bounds else int(prog.get("volume_count") or 1)


def tail_chars(text: str, n: int) -> str:
    text = text.rstrip()
    return text[-n:] if len(text) > n else text


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
    return {
        "title": prog.get("title"),
        "root": os.path.abspath(root),
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
    }


def jprint(obj) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, indent=2) + "\n")
