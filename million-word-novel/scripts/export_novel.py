#!/usr/bin/env python3
"""Export the novel: merge chapters into volumes + one book file, then zip.

  export_novel.py --root <project> [--zip] [--by-volume]

Produces under export/:
  book.txt           全书按章节顺序合并
  vol-1.txt ...      每卷一个文件（--by-volume）
  STATS.md           字数/章节/轮次报告
  <title>.zip        归档（--zip）
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _common import (  # noqa: E402
    book_chars,
    chapter_numbers,
    chapter_path,
    jprint,
    load_progress,
    paths,
    read_text,
    status_dict,
    volume_of,
    write_text,
)


def unsafe(name: str) -> str:
    for ch in '\\/:*?"<>|':
        name = name.replace(ch, "_")
    return name.strip() or "novel"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--zip", action="store_true")
    ap.add_argument("--by-volume", action="store_true")
    args = ap.parse_args()

    root = args.root
    prog = load_progress(root)
    p = paths(root)
    st = status_dict(root)
    os.makedirs(p["export"], exist_ok=True)

    nums = chapter_numbers(root)
    title = prog.get("title") or "未命名"

    book_parts = ["# %s\n\n> 全书合计 %s 字（口径 %s）\n\n" % (
        title, format(st["total_chars"], ","), st["count_mode"])]
    vol_parts = {}
    vol_counts = {}

    for n in nums:
        text = read_text(chapter_path(root, n)).strip()
        block = text + "\n\n"
        book_parts.append(block)
        v = volume_of(prog, n)
        vol_parts.setdefault(v, ["# %s · 第%d卷\n\n" % (title, v)])
        vol_parts[v].append(block)
        vol_counts[v] = vol_counts.get(v, 0) + 0

    book_file = os.path.join(p["export"], "book.txt")
    write_text(book_file, "".join(book_parts))

    produced = [book_file]
    if args.by_volume:
        for v in sorted(vol_parts):
            vf = os.path.join(p["export"], "vol-%d.txt" % v)
            write_text(vf, "".join(vol_parts[v]))
            produced.append(vf)

    stats_md = os.path.join(p["export"], "STATS.md")
    lines = [
        "# 《%s》生产报告\n" % title,
        "",
        "| 项目 | 数值 |",
        "|---|---|",
        "| 目标字数 | %s |" % format(st["target_chars"], ","),
        "| 实际字数 | %s |" % format(st["total_chars"], ","),
        "| 完成度 | %.2f%% |" % st["percent"],
        "| 章节数 | %d |",
        "| 卷数 | %d |" % st["volume_count"],
        "| 完成轮次 | %d |" % st["rounds_completed"],
        "| 统计口径 | %s |" % st["count_mode"],
        "| 导出时间 | %s |" % dt.datetime.now().replace(microsecond=0).isoformat(),
        "",
    ]
    lines[6] = "| 章节数 | %d |" % len(nums)
    write_text(stats_md, "\n".join(lines))
    produced.append(stats_md)

    zip_path = None
    if args.zip:
        zip_path = os.path.join(p["export"], "%s.zip" % unsafe(title))
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in produced:
                zf.write(f, os.path.basename(f))
            for n in nums:
                zf.write(chapter_path(root, n), "chapters/ch-%04d.txt" % n)
            for name in ("bible.md", "outline.md", "plot-ledger.md", "last-context.md", "progress.json"):
                fp = os.path.join(p["state"], name)
                if os.path.isfile(fp):
                    zf.write(fp, "state/%s" % name)
            zf.write(os.path.abspath(__file__), "scripts/export_novel.py")

    jprint({
        "ok": True,
        "chapters": len(nums),
        "total_chars": st["total_chars"],
        "export_dir": p["export"],
        "files": produced + ([zip_path] if zip_path else []),
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
