"""Docs/CLI agreement check used during the acceptance run.

Verifies, with no manual reading, that every command the documentation tells a
user to run actually exists, and that every script file it references is present.
"""
from __future__ import annotations

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import novel_state  # noqa: E402

DOCS = ("README.md", "SKILL.md", "docs/CLI.md", "docs/CHANGES.md",
        "docs/ARCHITECTURE.md", "docs/ACCEPTANCE.md")


def main() -> int:
    parser = novel_state.build_parser()
    choices = set(parser._subparsers._group_actions[0].choices.keys())
    cmd_pat = re.compile(r"novel_state\.py\s+([a-z][a-z-]+)")
    script_pat = re.compile(r"scripts[/\\]([A-Za-z_][A-Za-z0-9_]*)\.py")
    problems = []
    checked_cmds, checked_scripts = set(), set()
    for rel in DOCS:
        path = os.path.join(ROOT, rel)
        if not os.path.isfile(path):
            problems.append("%s 不存在" % rel)
            continue
        text = open(path, encoding="utf-8").read()
        for cmd in cmd_pat.findall(text):
            checked_cmds.add(cmd)
            if cmd not in choices:
                problems.append("%s: novel_state.py %s 不存在" % (rel, cmd))
        for name in script_pat.findall(text):
            checked_scripts.add(name)
            if not os.path.isfile(os.path.join(ROOT, "scripts", "%s.py" % name)):
                problems.append("%s: scripts/%s.py 不存在" % (rel, name))
        if "`n| " in text:
            problems.append("%s: 命令表被字面 `n| ` 污染" % rel)

    print("已核对子命令 %d 个：%s" % (len(checked_cmds), " ".join(sorted(checked_cmds))))
    print("已核对脚本 %d 个：%s" % (len(checked_scripts), " ".join(sorted(checked_scripts))))
    print("子命令总数（parser）：%d" % len(choices))
    missing_docs = [c for c in ("plan", "scene-plan", "audit", "commit", "round-done",
                                "next", "init", "recover", "rollback", "validate",
                                "extract", "auto-next", "migrate", "part", "import",
                                "status")
                    if c not in checked_cmds]
    if missing_docs:
        print("注意：以下子命令未被任何文档提及：%s" % missing_docs)
    if problems:
        print("FAIL：文档与 CLI 不一致")
        for p in problems:
            print("  - %s" % p)
        return 1
    print("PASS：文档中出现的每个命令与脚本都真实存在")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
