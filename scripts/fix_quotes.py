#!/usr/bin/env python3
"""Detect and repair dialogue lines that lost their quotation marks.

WHY: drafting through shell heredocs / nested quoting can silently strip the
`"` characters from dialogue. The result is prose that reads as:

    拿石头做什么。他问。
    记路。李禾说。

instead of:

    "拿石头做什么。"他问。
    "记路。"李禾说。

This is invisible to every other check (word count, repetition, banned
phrases) but makes the book unreadable. Run it on any chapter written through a
shell pipeline.

CLI CONTRACT (now identical to what README.md / SKILL.md / CLI.md say)
----------------------------------------------------------------------
    fix_quotes.py --file <chapter.txt>              report only, WRITES NOTHING
    fix_quotes.py --file <chapter.txt> --apply      rewrite the file
    fix_quotes.py --file <chapter.txt> --json       machine-readable report

Exit codes:
    0   nothing to repair, or repairs were applied with --apply
    1   repairs are pending and were NOT written (report-only mode)
    2   bad arguments / unreadable file

`--report-only` is kept as an explicit synonym of the default so both spellings
from older docs work; `--apply` is the only way this tool ever writes a byte.
"""
from __future__ import annotations

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _common import jprint, read_text, write_text  # noqa: E402

SPEAKERS = ("他", "她", "老赵", "沈默", "李禾", "周显", "小吴", "陈老师", "秦川",
            "苏晚", "李建国", "老太太", "陈嫂子", "周师傅", "刘建", "老宋")
VERBS = ("问", "说", "道", "答", "应", "喊", "叫")

_SP = "(?:%s)" % "|".join(SPEAKERS)
_VB = "(?:%s)" % "|".join(VERBS)

# Form 1: whole line is a speech whose tag sits at the end.
#   拿石头做什么。他问。   ->  "拿石头做什么。"他问。
PAT_TAIL = re.compile(
    r'^(?P<body>[^"\n]{2,200}?[。！？…])(?P<tag>%s%s[，。]?)$' % (_SP, _VB)
)

# Form 2: one line holds two speeches joined by an inline tag (the tag ends with
# a comma). This is what a shell heredoc produces when quotes are stripped:
#   我的。周师傅说，七七年收麦用的就是这把。
#   -> "我的。"周师傅说，"七七年收麦用的就是这把。"
PAT_MID = re.compile(
    r'^(?P<a>[^"\n]{2,200}?[。！？…])(?P<tag>%s%s，)(?P<b>[^"\n]{1,300})$' % (_SP, _VB)
)

# Form 3: three or more speeches, tag after each.
PAT_MULTI = re.compile(
    r'(?P<a>[^"\n]{2,200}?[。！？…])(?P<tag>%s%s，)(?P<b>[^"\n]{2,200}?[。！？…])(?P<tag2>%s%s，)(?P<c>[^"\n]{1,300})$'
    % (_SP, _VB, _SP, _VB)
)

# Form 4: narration clause, then a tag, then the speech. The narration ends with
# a comma, so the tag is unambiguous:
#   他下来的时候，老赵说，把鞋脱了。光着脚走下来的。
#   -> 他下来的时候，老赵说："把鞋脱了。光着脚走下来的。"
PAT_NARR = re.compile(
    r'^(?P<narr>[^"\n]{2,80}[，,])(?P<tag>%s%s，)(?P<b>[^"\n]{1,300})$' % (_SP, _VB)
)


NARRATION_CUES = ("提过一次", "说过", "写过", "记过", "意思是", "指的是", "提到",
                  "那句话", "这句话", "说的是", "意思")


def looks_like_indirect(a: str) -> bool:
    """True when the opening clause reads as narration ABOUT speech, not speech.

    Guards the most dangerous false positive: quoting an indirect sentence turns
    a narrator's summary into an in-world utterance, which changes the story.

    Also guards the mirror case, where the first clause is pure narration and the
    real speech follows the tag:

        爷爷没有回头。他说，走就走吧。
        -> must become  爷爷没有回头。他说："走就走吧。"
        NOT             "爷爷没有回头。"他说，"走就走吧。"
    """
    if any(cue in a for cue in NARRATION_CUES):
        return True
    if len(a) > 40 and "——" in a:
        return True
    # narration describing a body/act rather than an utterance
    if re.search(r'(回头|转身|站起|坐下|抬起|低下|走了|停下|愣住|点头|摇头|看着|望向|伸手)', a):
        return True
    return False


def repair_line(s: str):
    """Return (repaired_text, kind) or (None, None) if the line needs no repair."""
    if '"' in s or "“" in s:
        return None, None

    # three-part: A tag B tag C
    m = PAT_MULTI.match(s)
    if m:
        return ('"%s"%s"%s"%s"%s' % (m.group("a"), m.group("tag"),
                                      m.group("b"), m.group("tag2"), m.group("c")),
                "multi")

    # two-part: A tag B  — but ONLY when `a` reads like speech.
    # Indirect speech looks identical in shape and must not be quoted:
    #   第 238 号种子库——这个名字，李禾禾在卷二结尾提过一次。她说，守望 0 号在那里。
    # The tell is a narration verb (提过/说过/写过…) plus a long opening clause.
    m = PAT_MID.match(s)
    if m:
        a = m.group("a")
        if not looks_like_indirect(a):
            return ('"%s"%s"%s"' % (a, m.group("tag"), m.group("b")), "mid")

    # narration + tag + speech
    m = PAT_NARR.match(s)
    if m:
        return ('%s%s"%s"' % (m.group("narr"), m.group("tag"), m.group("b")), "narr")

    # tag at end
    m = PAT_TAIL.match(s)
    if m and len(m.group("body")) >= 3:
        return ('"%s"%s' % (m.group("body"), m.group("tag")), "tail")

    return None, None


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Repair dialogue lines that lost their quotation marks "
                    "(report-only unless --apply)")
    ap.add_argument("--file", required=True)
    ap.add_argument("--apply", action="store_true",
                    help="write the repairs (default: report only, no changes)")
    ap.add_argument("--report-only", action="store_true",
                    help="explicit synonym of the default (never writes)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if not os.path.isfile(args.file):
        sys.stderr.write("error: file not found: %s\n" % args.file)
        return 2

    text = read_text(args.file)
    lines = text.split("\n")
    fixed = []
    changes = []
    kinds = {"tail": 0, "mid": 0, "multi": 0, "narr": 0}

    for idx, ln in enumerate(lines):
        s = ln.strip()
        if not s:
            fixed.append(ln)
            continue
        new, kind = repair_line(s)
        if new:
            fixed.append(new)
            kinds[kind] += 1
            changes.append({"line": idx + 1, "kind": kind,
                            "before": s[:60], "after": new[:60]})
        else:
            fixed.append(ln)

    result = {"file": os.path.abspath(args.file), "repaired": len(changes),
              "kinds": kinds, "changes": changes, "applied": bool(args.apply),
              "mode": "apply" if args.apply else "report-only"}

    if args.apply:
        write_text(args.file, "\n".join(fixed))

    if args.json:
        jprint(result)
    else:
        print("%s对话引号: %d 处（句末 %d / 句中 %d / 多段 %d / 叙述+标签 %d）"
              % ("已修复" if args.apply else "待修复", len(changes),
                 kinds["tail"], kinds["mid"], kinds["multi"], kinds["narr"]))
        for c in changes[:14]:
            print("  L%-5d [%s] %s" % (c["line"], c["kind"], c["before"]))
            print("          -> %s" % c["after"])
        if changes and not args.apply:
            print("\n（未写入。确认无误后加 --apply 执行。）")
    return 1 if changes and not args.apply else 0


if __name__ == "__main__":
    raise SystemExit(main())
