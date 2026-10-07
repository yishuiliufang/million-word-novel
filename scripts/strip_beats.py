"""Remove or repair stage beats that polish_prose.fix_replies() inserted wrongly.

WHY THIS EXISTS:
`fix_replies()` replaces staccato bare replies ("是。") with a generic beat so the
page stops reading like a ping-pong match. But it is blind to who is speaking and
to what the reply actually meant. Measured damage in a real chapter:

    "你十九岁。"他说。
    她应了一声。          <- inserted; but the next line is a question, not a reply
    "你想了多久？"

    "两百三十八行里..."陈老师抬起头
    她应道。              <- inserted mid-exchange, speaker was 陈老师 (male)
    他点头。              <- another inserted beat

The fix: delete beats that sit *between* two dialogue lines (they interrupt an
exchange and add nothing), and demote the rest to a neutral narration line.
"""
import re
import sys

NEUTRAL_BEATS = (
    "他点了点头。", "她应了一声。", "他把头低下去。", "她没有反驳。",
    "他承认了。", "她承认了。", "他把手一挥。", "她把手收回去。",
    "他应道。", "她应道。", "他没有作声。", "她看了他一眼。",
    "他把纸推回去。", "她把杯子挪开。", "他站起来。", "她坐下来。",
    "他把话接了过去。", "她没有再问。", "她把手收回袖子里。",
    "他点头。", "她点头。", "他应了一声。",
)

# sentences that make a beat read as narration rather than a reply
DANGLING = re.compile(r"^(他|她)(应|承认|点头|站起|坐下|看|把|没有)")


def main():
    apply = "--apply" in sys.argv
    path = [a for a in sys.argv[1:] if not a.startswith("--")][0]
    lines = open(path, encoding="utf-8").read().split("\n")

    out = []
    removed = 0
    kept = 0
    kept_lines = []
    for i, ln in enumerate(lines):
        s = ln.strip()
        if s in NEUTRAL_BEATS:
            # previous non-empty output line
            prev = ""
            for j in range(len(out) - 1, -1, -1):
                if out[j].strip():
                    prev = out[j].strip()
                    break
            # next non-empty source line
            nxt = ""
            for j in range(i + 1, len(lines)):
                if lines[j].strip():
                    nxt = lines[j].strip()
                    break
            # A beat that sits inside a run of dialogue is an artifact: it was
            # generated from a bare "是。" that the exchange did not need.
            prev_dlg = '"' in prev
            next_dlg = '"' in nxt
            if prev_dlg and next_dlg:
                removed += 1
                continue
            kept += 1
        out.append(ln)

    print("%s: 删除打断对话的动作行 %d，保留 %d" % (path.split("\\")[-1], removed, kept))
    if apply:
        open(path, "w", encoding="utf-8", newline="\n").write("\n".join(out))
        print("  已写入")
    else:
        print("  （未写入，加 --apply 执行）")


if __name__ == "__main__":
    main()
