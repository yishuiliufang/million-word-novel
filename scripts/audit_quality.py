#!/usr/bin/env python3
"""Quality gate for the million-word novel (Level 1 + Level 2).

    audit_quality.py --root <project> [--chapter N] [--file draft] [--all] [--json] [--strict]

This is the historical entry point and its CLI is unchanged. The implementation
now lives in `narrative_audit.py`, because the old version only checked text
hygiene (length, cliches, 8-gram repetition, dialogue density) and could not see
a single semantic contradiction. Level 2 adds character/knowledge/timeline/
location/item/relationship/causality consistency and POSSIBLE_PLOT_REPETITION.

Two deliberate verdict changes:
  * 8-gram self-repetition is now an ADVISORY (the brief says n-gram checks may
    only be a supporting indicator). Only *extreme* repetition still blocks.
  * cross-chapter 8-gram overlap >12% is also an advisory, not a failure.
  Both are reported, both are visible in the brief and in round summaries.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import narrative_audit as na  # noqa: E402
from _common import chapter_numbers, jprint, load_progress  # noqa: E402


def legacy_shape(result: dict) -> dict:
    """Keep the pre-upgrade JSON report shape for existing consumers."""
    return {
        "chapter": result.get("chapter"),
        "exists": result.get("exists", True),
        "chars": result.get("chars"),
        "paragraphs": ((result.get("level1") or {}).get("metrics") or {}).get("paragraphs"),
        "dialogue": ((result.get("level1") or {}).get("metrics") or {}).get("dialogue"),
        "pass": bool(result.get("pass")),
        "issues": [b["message"] for b in result.get("blocking") or []],
        "advisories": [a["message"] for a in result.get("advisories") or []],
        "sha256": result.get("sha256"),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Quality gate (Level 1 hygiene + Level 2 consistency)")
    ap.add_argument("--root", required=True)
    ap.add_argument("--chapter", type=int, default=None)
    ap.add_argument("--file", default="", help="audit a draft instead of the committed chapter")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--strict", action="store_true", help="exit 1 on any failure")
    ap.add_argument("--level", default="both", choices=("1", "2", "both"))
    ap.add_argument("--delta", default="")
    ap.add_argument("--llm", default="")
    ap.add_argument("--legacy-json", action="store_true",
                    help="emit the pre-upgrade JSON report shape")
    args = ap.parse_args(argv)

    load_progress(args.root)  # fail fast with the historical error message
    nums = chapter_numbers(args.root)
    if args.chapter:
        targets = [args.chapter]
    elif args.all or not nums:
        targets = nums
    elif args.file:
        targets = []
    else:
        targets = nums[-1:]

    levels = {"1": (1,), "2": (2,), "both": (1, 2)}[args.level]
    results = []
    for n in targets:
        results.append(na.audit_chapter(args.root, n, file=args.file or None,
                                        levels=levels, llm_spec=args.llm))

    failed = [r for r in results if not r["pass"]]
    result = {
        "audited": len(results),
        "passed": len(results) - len(failed),
        "failed": len(failed),
        "all_pass": not failed,
        "reports": [legacy_shape(r) if args.legacy_json else r for r in results],
    }
    if args.legacy_json:
        result["reports_detailed"] = results

    if args.json:
        jprint(result)
        return 1 if (args.strict and failed) else 0

    for r in results:
        print(na.render_report(r))
    n_adv = sum(len(r["advisories"]) for r in results)
    print("\n合计：%d 审计，%d 通过，%d 阻断" % (result["audited"], result["passed"],
                                              result["failed"]))
    if n_adv:
        print("提示项 %d 条（辅助指标，不判失败；请在正文中人工确认）" % n_adv)
    if args.strict and failed:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
