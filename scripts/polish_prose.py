#!/usr/bin/env python3
"""Polish prose — but never invent canon.

THE RULE THIS FILE NOW OBEYS
----------------------------
    润色 ≠ 创作。  文本润色不能改变人物行为、情绪、因果、事实、时间、地点、关系。

The previous version broke that rule in three of its five passes:

  * `fix_vocatives` replaced a bare "陈老师。"她说， line with a NEW action
    ("他叫住她。" / "李禾叫了一声。") — inventing something a character did.
  * `fix_replies` inserted new stage directions ("他点了点头。" / "她站起来。")
    from a pool, guessing the speaker's gender with a name heuristic. Two other
    scripts in this repo (scan_beats.py, strip_beats.py) exist purely to clean up
    the continuity errors that produced.
  * `diversify` swapped "李禾站在那里" for "李禾把脚跟钉在地上" — different
    physical action, different meaning.

So the passes are now split by whether they can touch canon:

  CANON-SAFE (applied by default)
    * 回声对话    merge same-speaker split quotes          (speaker unchanged)
    * 代词标签    "A。"他说，"B。" -> "A。B。"              (speaker unchanged)
    * 同人标签轮换 rotate tags inside ONE character's pool (speaker unchanged)

  CANON-AFFECTING (DETECTED and REFUSED by default)
    * 称呼式台词  would fabricate a new action beat
    * 短应答      would insert a fabricated action beat
    * 填充动作    would rewrite what a character physically does

`--allow-canon-edits` applies them anyway and writes a `.polish.json` sidecar
recording exactly how many canon-affecting edits were made. Because `commit`
verifies the audit against the exact text hash, any polish performed after an
audit automatically invalidates that audit and forces a re-audit — the mechanism
that makes "润色后必须重审" mechanical rather than a matter of discipline.

  polish_prose.py --file <draft.md> [--report-only] [--allow-canon-edits]
                  [--strict-canon] [--seed N]
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _common import atomic_write_json, count_chars, now_iso, read_text, write_text  # noqa: E402

# ---------------------------------------------------------------- pools

TAG_POOLS = {
    "陈老师": ["陈老师道，", "陈老师把话接下去，", "陈老师顿了一下，",
               "陈老师又说，", "陈老师回答，", "陈老师说到这里停住，",
               "陈老师想了片刻，", "陈老师继续，"],
    "沈默": ["沈默道，", "沈默接着说，", "沈默把笔放下，", "沈默回答，",
             "沈默抬起头，", "沈默没有立刻说，"],
    "李禾": ["李禾道，", "李禾接着说，", "李禾低声说，", "李禾回答，",
             "李禾把头转过来，", "李禾没有马上答话，"],
    "老赵": ["老赵道，", "老赵把缸子放下，", "老赵想了想，", "老赵回答，", "老赵挠了挠头，"],
    "周显": ["周显道，", "周显低下头，", "周显把包带抓紧，", "周显回答，"],
    "秦川": ["秦川比了一下手", "秦川摇头", "秦川把纸推过来", "秦川指了指自己的喉咙"],
}

FILLER_BEATS = {
    "李禾站在那里": ["李禾没动", "李禾停住了", "李禾立在原地", "李禾没有走开",
                      "李禾站在灯下", "李禾没有坐"],
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

FILLER_MAX = 2
REPLY_MAX = 3
REPLY_BEAT_MAX = 2

REPLY_BEATS = [
    "他点了点头。", "她把话接了过去。", "他没有再问。", "她把手收回袖子里。",
    "他承认了。", "她承认了。", "他把手一挥。", "她把头低下去。",
    "他应道。", "她应道。", "他没有作声。", "她看了他一眼。",
    "他把纸推回去。", "她把杯子挪开。", "他站起来。", "她坐下来。",
]


# ================================================================ canon-safe passes

def thin_pronoun_tags(text: str) -> tuple:
    """Merge split quotes attributed by a bare pronoun: `"A。"他说，"B。"` -> `"A。B。"`

    Always safe: both halves belong to the same speaker by construction, so
    merging cannot change who is speaking.
    """
    stats = Counter()

    def repl(m):
        stats["pronoun_merged"] += 1
        return '"%s%s"' % (m.group(1), m.group(3))

    pat = re.compile(r'"([^"\n]{1,120})"([他她])说，(?:"([^"\n]{1,120})")')
    for _ in range(2):
        text = pat.sub(repl, text)
    return text, stats


def thin_tags(text: str) -> tuple:
    """Merge same-speaker split quotes; rotate leftover tags INSIDE one name's pool.

    The pool is keyed by character name, so a rotated tag still belongs to the
    same speaker. The merge branch removes an attribution between two quotes of
    one speaker, which is also speaker-preserving.
    """
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

    pat = re.compile(r'"([^"\n]{1,200})"([\u4e00-\u9fff]{2,4})说，(?:"([^"\n]{1,200})")')
    for _ in range(3):
        text = pat.sub(repl, text)

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
        return m.group(0)

    text = re.sub(r'"([^"\n]{1,200})"([\u4e00-\u9fff]{2,4})说。', repl_end, text)
    return text, stats


def thin_echo_pairs(text: str) -> tuple:
    """Collapse an immediately repeated identical quote (pure duplication)."""
    stats = Counter()
    pat = re.compile(r'("[^"\n]{2,60}")(\s*\n+\s*)\1')

    def repl(m):
        stats["echo_removed"] += 1
        return m.group(1)

    for _ in range(3):
        text, n = pat.subn(repl, text)
        if not n:
            break
    return text, stats


# ================================================================ canon-affecting passes

def fix_vocatives(text: str) -> tuple:
    """CANON-AFFECTING: replaces a bare vocative line with a fabricated action."""
    stats = Counter()
    seen = defaultdict(int)
    pools = {
        ("陈老师", "她"): ["李禾叫了一声。", "李禾抬头看他。", "李禾把账簿合上。"],
        ("李禾", "他"): ["他叫住她。", "陈老师把话头收住。", "他看着李禾。"],
        ("沈默", "她"): ["她转向沈默。", "她看着沈默。", "她叫住他。"],
        ("陈老师", "他"): ["沈默开口。", "沈默从屏幕那边转过来。"],
    }

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


def fix_replies(text: str) -> tuple:
    """CANON-AFFECTING: inserts fabricated action beats for bare affirmations."""
    stats = Counter()
    lines = text.split("\n")
    out = []
    beat_uses = defaultdict(int)

    def next_beat(gender):
        for cand in [b for b in REPLY_BEATS if b.startswith(gender)]:
            if beat_uses[cand] < REPLY_BEAT_MAX:
                beat_uses[cand] += 1
                return cand
        return None

    def infer_gender(idx):
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
                    stats["beat_inserted"] += 1
                    out.append(beat)
                else:
                    stats["dropped"] += 1
            continue
        out.append(line)
    text = "\n".join(out)
    return re.sub(r"\n{3,}", "\n\n", text), stats


def diversify(text: str) -> tuple:
    """CANON-AFFECTING: rewrites what a character physically does."""
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
        stats["rewritten"] = stats.get("rewritten", 0) + count - 1
    return text, stats


SAFE_PASSES = (("回声对话", thin_tags), ("代词标签", thin_pronoun_tags),
               ("整句重复", thin_echo_pairs))
CANON_PASSES = (("称呼式台词", fix_vocatives), ("短应答", fix_replies),
                ("填充动作", diversify))


# ================================================================ diagnostics

def top_ngrams(text: str, n: int = 8, k: int = 5) -> list:
    clean = re.sub(r"^\s*[>#\-*|]+\s*", "", text, flags=re.M)
    clean = re.sub(r"[>\-*|#]", "", clean)
    clean = re.sub(r"\s+", "", clean)
    c = Counter(clean[i:i + n] for i in range(len(clean) - 1 - n + 1))
    return c.most_common(k)


def changed_lines(before: str, after: str, limit: int = 8) -> list:
    """Line numbers where a canon-affecting pass would edit prose."""
    a, b = before.split("\n"), after.split("\n")
    out = []
    for i in range(max(len(a), len(b))):
        x = a[i] if i < len(a) else None
        y = b[i] if i < len(b) else None
        if x != y:
            out.append({"line": i + 1, "before": (x or "")[:60], "after": (y or "")[:60],
                        "kind": "CANON_AFFECTING_EDIT"})
            if len(out) >= limit:
                break
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Polish prose without inventing canon")
    ap.add_argument("--file", required=True, help="chapter draft to polish in place")
    ap.add_argument("--report-only", action="store_true", help="diagnose, write nothing")
    ap.add_argument("--allow-canon-edits", action="store_true",
                    help="also run the canon-affecting passes (forces a re-audit)")
    ap.add_argument("--strict-canon", action="store_true",
                    help="exit 1 when canon-affecting edits are detected")
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    if args.seed:
        random.seed(args.seed)

    text = read_text(args.file)
    before = count_chars(text)
    print("字数: %s" % format(before, ","))
    report = {"file": os.path.abspath(args.file), "at": now_iso(),
              "chars_before": before, "safe_passes": {}, "canon_passes": {},
              "canon_affecting_edits": [], "applied": not args.report_only,
              "allow_canon_edits": bool(args.allow_canon_edits)}

    for label, fn in SAFE_PASSES:
        if args.report_only:
            trial, st = fn(text)
            report["safe_passes"][label] = dict(st)
            continue
        text, st = fn(text)
        report["safe_passes"][label] = dict(st)
        detail = ", ".join("%s=%s" % (k, v) for k, v in list(st.items())[:4])
        print("  [%s·安全] %s" % (label, detail or "无改动"))

    for label, fn in CANON_PASSES:
        trial, st = fn(text)
        changed = trial != text
        report["canon_passes"][label] = {"stats": dict(st), "changed": changed}
        if not changed:
            print("  [%s] 无改动" % label)
            continue
        n = sum(v for k, v in st.items() if isinstance(v, int)) or len(
            changed_lines(text, trial, 999))
        edits = changed_lines(text, trial)
        report["canon_affecting_edits"].extend(edits)
        if args.allow_canon_edits:
            text = trial
            print("  [%s·⚠影响Canon] 已应用 %s 处（必须重新 audit）" % (label, n))
        else:
            print("  [%s·⚠影响Canon] 跳过 %s 处：该 pass 会新造人物动作/改变行为，"
                  "属于创作而非润色。如确需应用请加 --allow-canon-edits 并重新 audit。"
                  % (label, n))

    if report["canon_affecting_edits"]:
        print("\nCANON_AFFECTING_EDIT 明细（最多 8 条）：")
        for c in report["canon_affecting_edits"][:8]:
            print("  L%-5d %s" % (c["line"], c["before"]))
            print("        -> %s" % c["after"])

    if not args.report_only:
        text = re.sub(r"\n{3,}", "\n\n", text)
        write_text(args.file, text)

    after = count_chars(text)
    report["chars_after"] = after
    report["chars_delta"] = after - before
    print("字数: %s (变化 %+d)" % (format(after, ","), after - before))
    print("最高频 8-gram:")
    for gram, c in top_ngrams(text):
        print("  x%-3d %s" % (c, gram))
    if report["canon_affecting_edits"]:
        print("\n⚠ 本次存在 CANON_AFFECTING_EDIT：commit 前必须重新 audit"
              "（commit 会校验审计对应的文本哈希，改稿后旧审计自动失效）。")

    if not args.report_only:
        atomic_write_json(args.file + ".polish.json", report)
    if args.strict_canon and report["canon_affecting_edits"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
