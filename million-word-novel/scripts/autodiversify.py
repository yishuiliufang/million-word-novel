#!/usr/bin/env python3
"""Report the most repeated short phrases in a draft (diagnostic only).

WHY THIS IS REPORT-ONLY:
An earlier version rewrote repeated sentences automatically. It produced garbage:
"他把铲子插回土里" was replaced by a pooled line like "她把话接了过去", which
destroys narrative continuity and can invert who did what. Repetition is a
*semantic* problem — removing it requires knowing what the sentence is doing —
so the safe division of labour is:

  * polish_prose.py  -> mechanically safe passes (dialogue tags, vocatives,
                        bare replies, filler beats). Safe because it only
                        touches attribution, never action or fact.
  * this tool        -> tells you WHERE the repetition is, so the writer
                        rewrites those sentences by hand or per-chapter.

  autodiversify.py --file <draft.md> [--min-count 4]
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _common import count_chars, jprint, read_text  # noqa: E402

SIZES = (6, 8, 10, 12)


def clean(text: str) -> str:
    clean_text = re.sub(r"^\s*[>#\-*|]+\s*", "", text, flags=re.M)
    clean_text = re.sub(r"[>\-*|#]", "", clean_text)
    return re.sub(r"\s+", "", clean_text)


def top_ngrams(text: str, n: int, k: int = 10, min_count: int = 3) -> list:
    c = Counter(clean(text)[i:i + n] for i in range(len(clean(text)) - n + 1))
    return [(g, v) for g, v in c.most_common(k) if v >= min_count]


def sentences_containing(text: str, gram: str) -> list:
    """Return the sentences that contain `gram`, de-duplicated."""
    out = []
    for m in re.finditer(re.escape(gram), text):
        s = max(text.rfind("\n", 0, m.start()), text.rfind("。", 0, m.start())) + 1
        e = text.find("\n", m.end())
        if e < 0:
            e = len(text)
        frag = text[s:e].strip()
        if frag and frag not in out:
            out.append(frag)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True)
    ap.add_argument("--min-count", type=int, default=4,
                    help="only report phrases occurring at least this often")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    text = read_text(args.file)
    chars = count_chars(text)

    findings = []
    seen_grams = set()
    for size in SIZES:
        for gram, count in top_ngrams(text, size, 8, args.min_count):
            # skip if a longer already-reported gram covers this one
            if any(gram in g for g in seen_grams):
                continue
            seen_grams.add(gram)
            findings.append({
                "phrases": gram,
                "count": count,
                "sentences": sentences_containing(text, gram)[:4],
            })

    findings.sort(key=lambda f: -f["count"])
    result = {"file": os.path.abspath(args.file), "chars": chars,
              "threshold": args.min_count, "findings": findings}

    if args.json:
        jprint(result)
    else:
        print("字数: %s   报重阈值: ≥%d 次" % (format(chars, ","), args.min_count))
        if not findings:
            print("未发现超阈值重复。")
        for f in findings:
            print("\nx%-3d  %s" % (f["count"], f["phrases"]))
            for s in f["sentences"]:
                print("        %s" % s[:78])
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())

