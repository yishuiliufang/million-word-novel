#!/usr/bin/env python3
"""Report speech tags whose verb disagrees with the quote's content.

polish_prose.py rotates terminal tags from a per-character pool, which can glue
"他问。" onto a plain statement. This tool finds those, and ONLY those.

IMPORTANT — why the first version was wrong:
It flagged any quote ending in 。 that took 问. But this book (like much Chinese
fiction) punctuates questions with 。 as a style choice:
    "右手什么时候开始的。"李禾问。      <- correct, not an error
Out of 92 initial flags, only a handful were real, which makes the tool useless.

So the test is CONTENT-based: a quote may take 问 only if it carries an
interrogative cue (什么/吗/呢/哪/谁/怎么/多少/多久/几/是不是/有没有/为什么/干什么…).
A quote with no such cue and a declarative shape ("…找过我。"他问。) is a defect.

REPORT-ONLY by design: tags carry nuance, so a human decides the fix.
Exit code 1 when mismatches are found.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _common import read_text  # noqa: E402

# interrogative cues that justify a 问 tag even with 。 punctuation
CUE = ("什么", "吗", "呢", "哪", "谁", "怎么", "多少", "多久", "几", "是不是",
       "有没有", "为什么", "干什么", "得了", "行不行", "对不对", "好不好",
       "什么时候", "哪儿", "哪里", "怎样", "如何", "为何")

ASK_TAG = re.compile(r'^"[^"\n]*[。！](?:")(?:他|她|[\u4e00-\u9fff]{2,4})问[。，]?$')


def scan(path: str) -> list:
    lines = [l.strip() for l in read_text(path).split("\n") if l.strip()]
    hits = []
    for i, l in enumerate(lines):
        if not ASK_TAG.match(l):
            continue
        m = re.match(r'^"([^"\n]*)"', l)
        body = m.group(1) if m else ""
        if any(c in body for c in CUE):
            continue
        hits.append({"line": i + 1, "text": l})
    return hits


def main() -> int:
    ap = argparse.ArgumentParser(description="Report declarative quotes tagged with 问")
    ap.add_argument("files", nargs="+")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    total = 0
    all_hits = {}
    for path in args.files:
        if not os.path.isfile(path):
            sys.stderr.write("error: file not found: %s\n" % path)
            return 2
        hits = scan(path)
        all_hits[path] = hits
        total += len(hits)
        if hits and not args.json:
            print("%s: %d 处（陈述句误配『问』）" % (os.path.basename(path), len(hits)))
            for h in hits[:12]:
                print("    L%-5d %s" % (h["line"], h["text"][:74]))
    if args.json:
        sys.stdout.write(json.dumps({"files": all_hits, "total": total},
                                    ensure_ascii=False, indent=2) + "\n")
    else:
        print("\n合计 %d 处" % total)
    return 1 if total else 0


if __name__ == "__main__":
    raise SystemExit(main())
