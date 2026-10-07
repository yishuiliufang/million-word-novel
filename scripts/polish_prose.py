#!/usr/bin/env python3
"""Polish prose: break the four repetition diseases that ruin long AI-written novels.

Discovered while writing 《守望》: drafting at 16,000+ characters per chapter makes
the model lean on a handful of verbal tics. Each one is invisible in a 3,000-char
chapter and fatal across a million characters:

  1. ECHO DIALOGUE   "对。"沈默点头。"XXX。" repeated dozens of times.
  2. VOCATIVE LINES  "陈老师。"她说。 as a whole line, over and over.
  3. BARE REPLIES    "是。" on its own line, twenty-odd times.
  4. FILLER BEATS    "李禾站在那里" / "沈默把笔放下" as the only stage directions.

Run this BEFORE committing a chapter. It is idempotent and safe: it never
changes facts, only attribution and stage directions.

  polish_prose.py --file <draft.md> [--report-only] [--seed N]
"""
from __future__ import annotations

import argparse
import os
import random
import re
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _common import count_chars, read_text, write_text  # noqa: E402

# ---------------------------------------------------------------- pools

TAG_POOLS = {
    "陈老师": ["陈老师道，", "陈老师说得很慢，", "陈老师把话接下去，", "陈老师顿了一下，",
               "陈老师的声音低了些，", "陈老师又说，", "陈老师回答，", "陈老师说到这里停住，",
               "陈老师的语气没有变，", "陈老师想了片刻，", "陈老师继续，", "陈老师把话说完，"],
    "沈默": ["沈默道，", "沈默接着说，", "沈默看着屏幕，", "沈默把笔放下，", "沈默回答，",
             "沈默抬起头，", "沈默没有立刻说，"],
    "李禾": ["李禾道，", "李禾接着说，", "李禾把视线移过去，", "李禾低声说，", "李禾回答，",
             "李禾把头转过来，", "李禾没有马上答话，"],
    "老赵": ["老赵道，", "老赵把缸子放下，", "老赵想了想，", "老赵回答，", "老赵挠了挠头，"],
    "周显": ["周显道，", "周显低下头，", "周显把包带抓紧，", "周显回答，"],
    "秦川": ["秦川比了一下手", "秦川摇头", "秦川把纸推过来", "秦川指了指自己的喉咙"],
}

FILLER_BEATS = {
    "李禾站在那里": ["李禾没动", "李禾停住了", "李禾立在原地", "李禾没有走开",
                      "李禾站在灯下", "李禾把脚跟钉在地上", "李禾没有坐"],
    "沈默把笔放下": ["沈默搁下笔", "沈默松开笔", "沈默把笔推到一边", "沈默的手停在纸上",
                      "沈默把笔帽扣上"],
    "李禾低头看自己的手掌": ["李禾摊开手", "李禾把手举到灯下", "李禾看着自己的手心",
                              "李禾把手掌翻过来"],
    "她低头看自己的手掌": ["她摊开手", "她把手举到灯下", "她看着自己的手心", "她把手掌翻过来"],
    "低头看自己的手掌": ["摊开手", "看着自己的手心", "把手掌翻过来"],
    "陈老师看着她": ["陈老师的目光落在她脸上", "陈老师没有移开视线", "陈老师看了她一眼",
                      "陈老师望住她"],
    "陈老师看着他": ["陈老师把目光转到他脸上", "陈老师看了他一眼", "陈老师望住他"],
    "李禾看着他": ["李禾抬眼看他", "李禾把视线移过去", "李禾看了他一眼", "李禾的目光落在他脸上"],
    "陈老师没有回答": ["陈老师把嘴闭上", "陈老师没有作声", "陈老师不接这个话",
                        "陈老师把话咽了回去"],
    "陈老师把茶杯端起来": ["陈老师伸手去够杯子", "陈老师喝了口凉茶", "陈老师握住杯口"],
    "陈老师把茶杯放下": ["陈老师松开杯子", "陈老师把杯子推到一边", "陈老师的手离开杯沿"],
}

# a stage direction may appear at most this often before it is rotated
FILLER_MAX = 2
# bare affirmations kept as-is (the rest get merged or replaced)
REPLY_MAX = 3


# ---------------------------------------------------------------- passes

def thin_pronoun_tags(text: str) -> tuple:
    """Merge split quotes attributed by a bare pronoun.

    `"A。"他说，"B。"`  ->  `"A。B。"`

    This is always safe: merging two quotes from the same speaker cannot change
    who is speaking, and the attribution becomes unnecessary because Chinese
    dialogue alternates by convention and the surrounding action beats already
    identify the speaker.

    WHY IT MATTERS: the audit's 8-gram check cannot see `。"他说，"` (5 chars), so
    pronoun-tag spam slips through. Measured on real chapters, one 16,000-char
    draft carried 46x `他说，` + 25x `她说，` — the same disease as name-tag spam,
    which no other pass catches.
    """
    stats = Counter()

    def repl(m):
        a, pron, b = m.group(1), m.group(2), m.group(3)
        stats["pronoun_merged"] += 1
        return '"%s%s"' % (a, b)

    # both halves short enough that the merge reads naturally
    pat = re.compile(r'"([^"\n]{1,120})"([他她])说，(?:"([^"\n]{1,120})")')
    for _ in range(2):
        text = pat.sub(repl, text)
    return text, stats


def thin_tags(text: str) -> tuple:
    """Merge same-speaker split quotes (dropping the tag), rotate the rest."""
    counters = {k: defaultdict(int) for k in TAG_POOLS}
    stats = Counter()

    def repl(m):
        a, name, b = m.group(1), m.group(2), m.group(3)
        if len(a) + len(b) <= 120:
            stats["merged"] += 1
            return '"%s%s"' % (a, b)
        pool = TAG_POOLS.get(name)
        if pool:
            for cand in pool:
                if counters[name][cand] < 2:
                    counters[name][cand] += 1
                    stats["rotated"] += 1
                    return '"%s"%s"%s"' % (a, cand, b)
        stats["kept"] += 1
        return m.group(0)

    # group(3) is the inner text of the second quote, without its quotes.
    pat = re.compile(r'"([^"\n]{1,200})"([\u4e00-\u9fff]{2,4})说，(?:"([^"\n]{1,200})")')
    for _ in range(3):  # merging exposes new mergeable pairs
        text = pat.sub(repl, text)

    # terminal tags:  "XXX。"陈老师说。  ->  "XXX。"他说。 / rotated beat
    end_counters = defaultdict(int)

    def repl_end(m):
        quote, name = m.group(1), m.group(2)
        pool = TAG_POOLS.get(name) or []
        for cand in pool:
            stem = cand.rstrip("，")
            if end_counters[stem] < 2:
                end_counters[stem] += 1
                stats["terminal_rotated"] += 1
                return '"%s"%s。' % (quote, stem)
        stats["terminal_kept"] += 1
        return '"%s"他说。' % quote

    text = re.sub(r'"([^"\n]{1,200})"([\u4e00-\u9fff]{2,4})说。', repl_end, text)
    return text, stats


def fix_vocatives(text: str) -> tuple:
    """Turn repeated bare-vocative lines into action beats."""
    stats = Counter()
    seen = defaultdict(int)
    pools = {k: list(v) for k, v in {
        ("陈老师", "她"): ["李禾叫了一声。", "李禾抬头看他。", "李禾把账簿合上。"],
        ("李禾", "他"): ["他叫住她。", "陈老师把话头收住。", "他看着李禾。"],
        ("沈默", "她"): ["她转向沈默。", "她看着沈默。", "她叫住他。"],
        ("陈老师", "他"): ["沈默开口。", "沈默从屏幕那边转过来。"],
    }.items()}

    # "陈老师。"她说，"真正的台词"  ->  "真正的台词"他说。
    def repl2(m):
        name, pron, body = m.group(1), m.group(2), m.group(3)
        seen[("v2", name, pron)] += 1
        if seen[("v2", name, pron)] <= 1:
            return m.group(0)
        stats["vocative_inline"] += 1
        end = {"他": ["他说。", "他问。", "他把话说完。", "他没有再往下说。"],
               "她": ["她说。", "她问。", "她把话说出口。", "她没有退让。"]}[pron]
        return "%s%s" % (body, end[(seen[("v2", name, pron)] - 2) % len(end)])

    text = re.sub(r'^"([\u4e00-\u9fff]{2,4})。"([他她])说，(".*")$', repl2, text, flags=re.M)

    def repl(m):
        name, pron = m.group(1), m.group(2)
        seen[(name, pron)] += 1
        if seen[(name, pron)] <= 1:
            return m.group(0)
        stats["vocative_line"] += 1
        pool = pools.get((name, pron))
        if pool:
            return pool.pop(0)
        return "他没有立刻接话。" if pron == "他" else "她没有立刻接话。"

    text = re.sub(r'^"([\u4e00-\u9fff]{2,4})。"([他她])说。$', repl, text, flags=re.M)
    return text, stats


REPLY_BEATS = [
    "他点了点头。", "她把话接了过去。", "他没有再问。", "她把手收回袖子里。",
    "他承认了。", "她承认了。", "他把手一挥。", "她把头低下去。",
    "他应道。", "她应道。", "他没有作声。", "她看了他一眼。",
    "他把纸推回去。", "她把杯子挪开。", "他站起来。", "她坐下来。",
]

# Cap per-beat reuse: with 16 beats and a 2-use cap, a chapter can absorb 32
# replacements before any single beat reads as a tic. Beyond that we fall back
# to merging into the neighbouring line instead of inserting a new one.
REPLY_BEAT_MAX = 2


def fix_replies(text: str) -> tuple:
    """Merge or replace staccato bare affirmations.

    SAFETY: a beat is only inserted when the speaker can be inferred from a name
    in the preceding lines. If no name is available the line is MERGED into the
    previous speech instead of inventing a pronoun — otherwise the pass inserts
    "他点了点头。" into a conversation between two women, which is a continuity
    error readers notice immediately (observed: 11 such insertions across 6
    chapters before this guard was added).
    """
    stats = Counter()
    lines = text.split("\n")
    out = []
    beat_uses = defaultdict(int)

    def next_beat(gender):
        cands = [b for b in REPLY_BEATS if b.startswith(gender)]
        for cand in cands:
            if beat_uses[cand] < REPLY_BEAT_MAX:
                beat_uses[cand] += 1
                return cand
        return None

    def infer_gender(idx):
        """Return 他/她/None by scanning back for the nearest named speaker."""
        window = " ".join(x.strip() for x in lines[max(0, idx - 8):idx])
        female = ("李禾" in window) or ("陈嫂子" in window) or ("老太太" in window) \
            or ("苏晚" in window)
        male = ("李禾" not in window) and any(
            m in window for m in ("老赵", "沈默", "周显", "秦川", "陈老师",
                                  "刘建", "周师傅", "老宋", "李建国"))
        if male and not female:
            return "他"
        if female and not male:
            return "她"
        return None

    for li, line in enumerate(lines):
        s = line.strip()
        if s in ('"是。"', '"对。"', '"是的。"', '"嗯。"', '"好。"'):
            stats["bare_reply"] += 1
            if stats["bare_reply"] <= REPLY_MAX:
                out.append(line)
                continue
            merged = False
            if out and out[-1].rstrip().endswith('"') and len(out[-1]) < 40:
                out[-1] = out[-1].rstrip() + s.strip('"')
                merged = True
            if not merged:
                gender = infer_gender(li)
                beat = next_beat(gender) if gender else None
                if beat:
                    out.append(beat)
                else:
                    # No safe gender, or pool exhausted: DROP the line rather than
                    # invent an attribution. A bare "是。" carries no information.
                    stats["dropped"] += 1
            continue
        out.append(line)
    text = "\n".join(out)
    return re.sub(r"\n{3,}", "\n\n", text), stats


def diversify(text: str) -> tuple:
    """Rotate over-used filler stage directions."""
    stats = Counter()
    for phrase, alts in FILLER_BEATS.items():
        count = text.count(phrase)
        if count <= FILLER_MAX:
            continue
        first = text.find(phrase)
        head = text[:first + len(phrase)]
        tail = text[first + len(phrase):]
        idx = 0
        while phrase in tail:
            tail = tail.replace(phrase, alts[idx % len(alts)], 1)
            idx += 1
        text = head + tail
        stats[phrase] = count - 1
    return text, stats


def top_ngrams(text: str, n: int = 8, k: int = 5) -> list:
    clean = re.sub(r"^\s*[>#\-*|]+\s*", "", text, flags=re.M)
    clean = re.sub(r"[>\-*|#]", "", clean)
    clean = re.sub(r"\s+", "", clean)
    c = Counter(clean[i:i + n] for i in range(len(clean) - n + 1))
    return c.most_common(k)


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True, help="chapter draft to polish in place")
    ap.add_argument("--report-only", action="store_true")
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    if args.seed:
        random.seed(args.seed)

    text = read_text(args.file)
    before = count_chars(text)
    print("字数: %s" % format(before, ","))

    passes = [("回声对话", thin_tags), ("代词标签", thin_pronoun_tags),
              ("称呼式台词", fix_vocatives), ("短应答", fix_replies),
              ("填充动作", diversify)]
    for label, fn in passes:
        if args.report_only:
            continue
        text, st = fn(text)
        detail = ", ".join("%s=%s" % (k, v) for k, v in list(st.items())[:4])
        print("  [%s] %s" % (label, detail or "无改动"))

    if not args.report_only:
        text = re.sub(r"\n{3,}", "\n\n", text)
        write_text(args.file, text)

    after = count_chars(text)
    print("字数: %s (变化 %+d)" % (format(after, ","), after - before))
    print("最高频 8-gram:")
    for gram, c in top_ngrams(text):
        print("  x%-3d %s" % (c, gram))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
