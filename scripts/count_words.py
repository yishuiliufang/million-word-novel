#!/usr/bin/env python3
"""Count novel length.

  count_words.py --root <project> [--mode no_ws|cjk|all] [--json] [--per-chapter]

Default mode `no_ws` counts every non-whitespace character (the usual Chinese
web-novel platform 字数 口径). `cjk` counts only Han ideographs. `all` counts raw
characters including whitespace.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _common import (  # noqa: E402
    COUNT_MODES,
    chapter_numbers,
    chapter_path,
    count_chars,
    jprint,
    load_progress,
    read_text,
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--mode", choices=COUNT_MODES, default=None)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--per-chapter", action="store_true")
    args = ap.parse_args()

    prog = load_progress(args.root)
    mode = args.mode or prog.get("count_mode", "no_ws")
    target = int(prog.get("target_chars", 1_000_000))

    rows = []
    total = 0
    for n in chapter_numbers(args.root):
        p = chapter_path(args.root, n)
        text = read_text(p) if os.path.isfile(p) else ""
        c = count_chars(text, mode)
        total += c
        rows.append({"chapter": n, "chars": c})

    result = {
        "mode": mode,
        "target_chars": target,
        "total_chars": total,
        "remaining": max(0, target - total),
        "percent": round(total * 100.0 / target, 2) if target else 0.0,
        "chapters": len(rows),
        "complete": total >= target,
    }
    if args.per_chapter:
        result["per_chapter"] = rows

    if args.json:
        jprint(result)
    else:
        print("字数统计（口径 %s）" % mode)
        print("  已写   : %s 字" % format(total, ","))
        print("  目标   : %s 字" % format(target, ","))
        print("  剩余   : %s 字" % format(max(0, target - total), ","))
        print("  进度   : %.2f%%" % result["percent"])
        print("  章节数 : %d" % len(rows))
        if args.per_chapter:
            for r in rows:
                print("    ch-%04d  %s 字" % (r["chapter"], format(r["chars"], ",")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
