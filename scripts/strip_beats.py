#!/usr/bin/env python3
"""Remove action beats that interrupt a dialogue exchange (repair tool).

`polish_prose.fix_replies()` used to insert generic beats between two lines of
dialogue, which reads as a fake pause and can attribute an action to the wrong
speaker. That pass is now CANON-AFFECTING and off by default; this tool exists to
repair drafts written before the change (or produced with --allow-canon-edits).

  strip_beats.py <chapter> [--apply] [--json]

Without --apply nothing is written. Exit code 1 when interruptions are found and
were not removed.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _common import read_text, write_text  # noqa: E402

NEUTRAL_BEATS = (
    "他点了点头。", "她应了一声。", "他把头低下去。", "她没有反驳。",
    "他承认了。", "她承认了。", "他把手一挥。", "她把手收回去。",
    "他应道。", "她应道。", "他没有作声。", "她看了他一眼。",
    "他把纸推回去。", "她把杯子挪开。", "他站起来。", "她坐下来。",
    "他把话接了过去。", "她没有再问。", "她把手收回袖子里。",
    "他点头。", "她点头。", "他应了一声。",
)

DANGLING = re.compile(r"^(他|她)(应|承认|点头|站起|坐下|看|把|没有)")


def process(text: str) -> tuple:
    lines = text.split("\n")
    out, removed, kept = [], [], []
    for i, ln in enumerate(lines):
        s = ln.strip()
        if s in NEUTRAL_BEATS:
            prev = ""
            for j in range(len(out) - 1, -1, -1):
                if out[j].strip():
                    prev = out[j].strip()
                    break
            nxt = ""
            for j in range(i + 1, len(lines)):
                if lines[j].strip():
                    nxt = lines[j].strip()
                    break
            if '"' in prev and '"' in nxt:
                removed.append({"line": i + 1, "text": s})
                continue
            kept.append({"line": i + 1, "text": s})
        out.append(ln)
    return "\n".join(out), removed, kept


def main() -> int:
    ap = argparse.ArgumentParser(description="Remove dialogue-interrupting action beats")
    ap.add_argument("file")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if not os.path.isfile(args.file):
        sys.stderr.write("error: file not found: %s\n" % args.file)
        return 2

    text = read_text(args.file)
    new, removed, kept = process(text)
    result = {"file": os.path.abspath(args.file), "removed": len(removed),
              "kept": len(kept), "applied": bool(args.apply),
              "removed_lines": removed[:20]}
    if args.apply and removed:
        write_text(args.file, new)
    if args.json:
        sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    else:
        print("%s: 删除打断对话的动作行 %d，保留 %d"
              % (os.path.basename(args.file), len(removed), len(kept)))
        if removed and not args.apply:
            print("  （未写入，加 --apply 执行）")
        elif removed:
            print("  已写入")
    return 1 if (removed and not args.apply) else 0


if __name__ == "__main__":
    raise SystemExit(main())
