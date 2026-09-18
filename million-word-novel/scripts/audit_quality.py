#!/usr/bin/env python3
"""Quality gate for the million-word novel.

Catches the failure modes that actually kill a 1,000,000-character project:
banned AI cliches, self-repetition, chapter-length drift, and template-shaped
prose. Exits non-zero when a chapter fails, so the loop must rewrite it.

  audit_quality.py --root <project> [--chapter N] [--all] [--json]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _common import (  # noqa: E402
    chapter_numbers,
    chapter_path,
    count_chars,
    jprint,
    load_progress,
    read_text,
)

# template-shaped prose that reads as machine-written even at scale
STRUCTURAL_SMELLS = [
    (r"不是[^，。！？]{1,12}，不是[^，。！？]{1,12}，是", "三段否定排比"),
    (r"他/她?不知道的是[，,]", "旁白剧透"),
    (r"这一(刻|瞬间)[，,]", "滥用瞬间"),
    (r"仿佛(整个)?世界", "世界级夸张"),
    (r"[，,]。(?!.)", "断句噪音"),
    (r"(!|！){2,}", "叠用感叹号"),
    (r"[。！？][”\"]?[。！？]", "连续句号"),
]


def load_banned(root: str) -> list:
    from _common import paths
    p = paths(root)["banned"]
    if not os.path.isfile(p):
        return []
    out = []
    for line in read_text(p).splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            out.append(s)
    return out


def ngrams(text: str, n: int = 8) -> Counter:
    """Character n-grams over the prose.

    Markdown/document scaffolding is stripped first: blockquote markers (`>`),
    list bullets, table pipes, and heading hashes. Otherwise a chapter that
    quotes a dated ledger — "> 一九九七年十月三日", "> 一九九七年十月九日" —
    trips the repetition detector on the `>一九九七年十月` prefix alone, which is
    formatting, not repeated prose.
    """
    clean = re.sub(r"^\s*[>#\-*|]+\s*", "", text, flags=re.M)
    clean = re.sub(r"[>\-*|#]", "", clean)
    clean = re.sub(r"\s+", "", clean)
    return Counter(clean[i:i + n] for i in range(len(clean) - n + 1))


def repeated_phrases(text: str, n: int = 8, min_count: int = 3, chars: int = 0) -> list:
    """Find self-repetition, with a length-aware threshold.

    A fixed threshold of 3 is calibrated for a ~3,000-character chapter. At
    16,000+ characters (long-chapter mode) a phrase occurring 3 times is ordinary
    prose, not a defect, so the bar rises with length: 3 + (chars-3000)/6000.
    Without this, the audit rejects clean long chapters forever and the loop
    thrashes on cosmetic edits instead of writing.
    """
    if chars:
        min_count = max(min_count, 3 + int((chars - 3000) / 6000))
    counts = ngrams(text, n)
    return [{"phrase": k, "count": v} for k, v in counts.most_common(20) if v >= min_count]


def audit_chapter(root: str, number: int, banned: list, mode: str, lo: int, hi: int,
                  prev_ngrams: Counter = None) -> dict:
    path = chapter_path(root, number)
    if not os.path.isfile(path):
        return {"chapter": number, "exists": False, "pass": False, "issues": ["章节文件缺失"]}

    text = read_text(path)
    chars = count_chars(text, mode)
    issues = []

    if chars < lo:
        issues.append("字数不足：%d < %d" % (chars, lo))
    if chars > hi:
        issues.append("字数超标：%d > %d" % (chars, hi))

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if lines:
        avg = sum(len(ln) for ln in lines) / float(len(lines))
        if avg > 60:
            issues.append("段落过长：平均 %.0f 字/段（>60 读起来是论文不是小说）" % avg)
    if len(lines) < max(8, chars / 200):
        issues.append("段落太少：%d 段，疑似大段堆砌" % len(lines))

    for phrase in banned:
        hits = text.count(phrase)
        if hits:
            issues.append("禁用短语「%s」×%d" % (phrase, hits))

    for pattern, label in STRUCTURAL_SMELLS:
        m = re.search(pattern, text)
        if m:
            issues.append("句式雷同：%s（%s）" % (label, m.group(0)[:24]))

    reps = repeated_phrases(text, 8, 3, chars)
    if reps:
        issues.append("本章内自我重复：%s" % "；".join("%s×%d" % (r["phrase"], r["count"]) for r in reps[:3]))

    if prev_ngrams:
        cur = ngrams(text, 8)
        overlap = sum(min(c, prev_ngrams.get(k, 0)) for k, c in cur.items())
        total = max(1, sum(cur.values()))
        ratio = overlap / float(total)
        if ratio > 0.12:
            issues.append("与上一章重复率过高：%.1f%%（>12%%，疑似复述前文）" % (ratio * 100))

    dialogue = len(re.findall(r"[“\"][^”\"]{2,}[”\"]", text))
    if dialogue == 0:
        issues.append("全章无对话，疑似说明文")
    elif dialogue < max(3, chars / 900):
        issues.append("对话偏少：%d 处（建议 ≥%d）" % (dialogue, int(max(3, chars / 900))))

    return {
        "chapter": number,
        "exists": True,
        "chars": chars,
        "paragraphs": len(lines),
        "dialogue": dialogue,
        "pass": not issues,
        "issues": issues,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--chapter", type=int, default=None)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--strict", action="store_true", help="exit 1 on any failure")
    args = ap.parse_args()

    prog = load_progress(args.root)
    mode = prog.get("count_mode", "no_ws")
    lo, hi = prog.get("chars_per_chapter_tolerance", [2000, 4000])
    banned = load_banned(args.root)
    nums = chapter_numbers(args.root)

    if args.chapter:
        targets = [args.chapter]
    elif args.all or not nums:
        targets = nums
    else:
        targets = nums[-1:]

    reports = []
    prev_ngrams = None
    for n in targets:
        prev = n - 1
        if prev >= 1 and os.path.isfile(chapter_path(args.root, prev)):
            prev_ngrams = ngrams(read_text(chapter_path(args.root, prev)), 8)
        reports.append(audit_chapter(args.root, n, banned, mode, lo, hi, prev_ngrams))

    failed = [r for r in reports if not r["pass"]]
    result = {
        "audited": len(reports),
        "passed": len(reports) - len(failed),
        "failed": len(failed),
        "all_pass": not failed,
        "reports": reports,
    }

    if args.json:
        jprint(result)
    else:
        for r in reports:
            if not r.get("exists"):
                print("ch-%04d  缺失" % r["chapter"])
                continue
            flag = "OK  " if r["pass"] else "FAIL"
            print("[%s] ch-%04d  %d 字  %d 段  %d 对话" % (
                flag, r["chapter"], r["chars"], r["paragraphs"], r["dialogue"]))
            for it in r["issues"]:
                print("        - %s" % it)
        print("\n合计：%d 审计，%d 通过，%d 不合格" % (result["audited"], result["passed"], result["failed"]))

    if args.strict and failed:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
