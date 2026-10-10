#!/usr/bin/env python3
"""Find stage beats whose implied speaker disagrees with the surrounding dialogue.

`polish_prose.fix_replies()` can insert a generic beat like "他点了点头。" — but
since the 2.0 rewrite that pass is CANON-AFFECTING and is skipped by default, so
this tool is now a *diagnostic for drafts polished with --allow-canon-edits* (and
for legacy chapters polished by the old version). It reads, never writes.

  scan_beats.py <chapter> [--json]

Exit code 1 when suspicious beats are found, so it can gate a round.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _common import read_text  # noqa: E402

BEATS = ("他点了点头。", "她应了一声。", "他把头低下去。", "她没有反驳。",
         "他承认了。", "她承认了。", "他把手一挥。", "她把手收回去。",
         "他应道。", "她应道。", "他没有作声。", "她看了他一眼。",
         "他把纸推回去。", "她把杯子挪开。", "他站起来。", "她坐下来。",
         "他把话接了过去。", "她没有再问。", "她把手收回袖子里。",
         "他点头。", "她点头。", "他应了一声。")

MALE = ("李建国", "老赵", "沈默", "周显", "秦川", "陈老师", "刘建", "周师傅", "老宋")


def scan(path: str) -> list:
    lines = [l.strip() for l in read_text(path).split("\n") if l.strip()]
    hits = []
    for i, l in enumerate(lines):
        if l not in BEATS:
            continue
        ctx = " ".join(lines[max(0, i - 6):i])
        speaker_is_male = any(m in ctx for m in MALE)
        female_ctx = ("陈嫂子" in ctx or "老太太" in ctx or "苏晚" in ctx or "李禾" in ctx)
        if l.startswith("他") and female_ctx and not speaker_is_male:
            hits.append({"line": i + 1, "text": l,
                         "why": "疑似性别错误（上下文只有女性说话人）"})
        elif l.startswith("她") and speaker_is_male and not female_ctx:
            hits.append({"line": i + 1, "text": l,
                         "why": "疑似性别错误（上下文只有男性说话人）"})
    return hits


def main() -> int:
    ap = argparse.ArgumentParser(description="Report suspicious inserted action beats")
    ap.add_argument("files", nargs="+")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    all_hits = {}
    for path in args.files:
        if not os.path.isfile(path):
            sys.stderr.write("error: file not found: %s\n" % path)
            return 2
        hits = scan(path)
        all_hits[path] = hits
        if hits:
            print("%s: 可疑动作行 %d" % (os.path.basename(path), len(hits)))
            for h in hits[:20]:
                print("  L%-5d %s  <- %s" % (h["line"], h["text"], h["why"]))
    total = sum(len(v) for v in all_hits.values())
    if args.json:
        sys.stdout.write(json.dumps({"files": all_hits, "total": total},
                                    ensure_ascii=False, indent=2) + "\n")
    else:
        print("可疑动作行: %d" % total)
    return 1 if total else 0


if __name__ == "__main__":
    raise SystemExit(main())
