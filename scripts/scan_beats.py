"""Find stage beats inserted by polish_prose.py that disagree with the speaker.

polish_prose.fix_replies() replaces bare "是。"/"对。" lines with a generic beat
like "他点了点头。" — but it cannot know the speaker, so it can insert a male
beat into a conversation between two women. That is a continuity error a reader
will notice immediately.

Heuristic: a beat line is suspect when it is immediately surrounded by dialogue
from speakers of the opposite gender, or when the chapter has no male speaker
present at all.
"""
import re
import sys

BEATS = ("他点了点头。", "她应了一声。", "他把头低下去。", "她没有反驳。",
         "他承认了。", "她承认了。", "他把手一挥。", "她把手收回去。",
         "他应道。", "她应道。", "他没有作声。", "她看了他一眼。",
         "他把纸推回去。", "她把杯子挪开。", "他站起来。", "她坐下来。",
         "他把话接了过去。", "她没有再问。", "她把手收回袖子里。")

MALE = ("李建国", "老赵", "沈默", "周显", "秦川", "陈老师", "刘建", "周师傅", "老宋")


def main():
    path = sys.argv[1]
    lines = [l.strip() for l in open(path, encoding="utf-8").read().split("\n") if l.strip()]
    hits = []
    for i, l in enumerate(lines):
        if l not in BEATS:
            continue
        # look back for the nearest speaker attribution
        ctx = " ".join(lines[max(0, i - 6):i])
        speaker_is_male = any(m in ctx for m in MALE)
        female_ctx = ("陈嫂子" in ctx or "老太太" in ctx or "苏晚" in ctx or "李禾" in ctx)
        if l.startswith("他") and female_ctx and not speaker_is_male:
            hits.append((i + 1, l, "疑似性别错误（上下文只有女性说话人）"))
        elif l.startswith("她") and speaker_is_male and not female_ctx:
            hits.append((i + 1, l, "疑似性别错误（上下文只有男性说话人）"))
    print("可疑动作行: %d" % len(hits))
    for ln, text, why in hits:
        print("  L%-5d %s  <- %s" % (ln, text, why))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
