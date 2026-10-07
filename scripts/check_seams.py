#!/usr/bin/env python3
"""Detect duplication at the seams between consecutive part files.

Long chapters are drafted as parts (part 1, part 2, ...) and concatenated.
The classic failure: the previous part ends with a line and the next part opens
with the same line, because the drafter lost track of where it stopped. The
result reads as a stutter and the repetition audit flags it.

  check_seams.py --dir <parts-dir> [--window 3]
"""
from __future__ import annotations

import argparse
import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _common import jprint, read_text  # noqa: E402


def meaningful_lines(text: str) -> list:
    return [ln.strip() for ln in text.split("\n") if ln.strip()]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True, help="directory holding part files")
    ap.add_argument("--pattern", default="*.txt")
    ap.add_argument("--window", type=int, default=3)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    files = sorted(glob.glob(os.path.join(args.dir, args.pattern)))
    if len(files) < 2:
        print("需要至少两个分节文件才能检查接缝（找到 %d 个）" % len(files))
        return 0

    problems = []
    for i in range(len(files) - 1):
        a, b = files[i], files[i + 1]
        tail = meaningful_lines(read_text(a))[-args.window:]
        head = meaningful_lines(read_text(b))[:args.window]
        overlap = []
        for line in tail:
            if line in head:
                overlap.append(line)
        # also catch a duplicated opening sentence pair (not exact line match)
        tail_blob = "".join(tail)
        head_blob = "".join(head)
        for size in (30, 20):
            for start in range(0, max(1, len(tail_blob) - size + 1)):
                frag = tail_blob[start:start + size]
                if len(frag) == size and frag in head_blob:
                    overlap.append("片段重复：%s…" % frag[:24])
                    break
            if overlap:
                break
        if overlap:
            problems.append({
                "from": os.path.basename(a),
                "to": os.path.basename(b),
                "overlap": sorted(set(overlap)),
            })

    result = {"files": len(files), "seams": len(files) - 1,
              "problems": problems, "clean": not problems}

    if args.json:
        jprint(result)
    else:
        if not problems:
            print("接缝检查通过：%d 个分节，%d 处接缝无重复。" % (len(files), len(files) - 1))
        else:
            print("发现 %d 处接缝重复——请修改分节源文件，不要只改 staging：" % len(problems))
            for p in problems:
                print("  %s -> %s" % (p["from"], p["to"]))
                for o in p["overlap"]:
                    print("      %s" % o)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
