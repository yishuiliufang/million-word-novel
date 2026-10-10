#!/usr/bin/env python3
"""Test runner: prints machine-checkable PASS/FAIL counts and writes a JSON report.

    python tests/run_tests.py                  # run everything
    python tests/run_tests.py test_workflow    # run one module
    python tests/run_tests.py --report out.json
    python tests/run_tests.py --clean          # remove .tmp-tests afterwards

Exit code 0 only when every test passed, so CI can gate on it.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for path in (ROOT, HERE, os.path.join(ROOT, "scripts")):
    if path not in sys.path:
        sys.path.insert(0, path)

TMP_ROOT = os.path.join(ROOT, ".tmp-tests")


class Result(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.records = []

    def addSuccess(self, test):
        super().addSuccess(test)
        self.records.append({"test": test.id(), "status": "PASS"})

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self.records.append({"test": test.id(), "status": "FAIL",
                             "detail": self._exc_info_to_string(err, test)[-1200:]})

    def addError(self, test, err):
        super().addError(test, err)
        self.records.append({"test": test.id(), "status": "ERROR",
                             "detail": self._exc_info_to_string(err, test)[-1200:]})

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self.records.append({"test": test.id(), "status": "SKIP", "detail": reason})


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Run the million-word-novel test suite")
    ap.add_argument("modules", nargs="*", help="test module names (default: all)")
    ap.add_argument("--report", default=os.path.join(ROOT, "docs", "test-report.json"))
    ap.add_argument("--clean", action="store_true", help="remove .tmp-tests afterwards")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    loader = unittest.TestLoader()
    if args.modules:
        suite = unittest.TestSuite()
        for name in args.modules:
            suite.addTests(loader.loadTestsFromName(name))
    else:
        suite = loader.discover(HERE, pattern="test_*.py", top_level_dir=HERE)

    started = time.time()
    stream = sys.stderr if args.verbose else io.StringIO()
    runner = unittest.TextTestRunner(stream=stream, verbosity=2 if args.verbose else 1,
                                     resultclass=Result)
    result = runner.run(suite)
    elapsed = time.time() - started

    records = getattr(result, "records", [])
    passed = sum(1 for r in records if r["status"] == "PASS")
    failed = sum(1 for r in records if r["status"] in ("FAIL", "ERROR"))
    skipped = sum(1 for r in records if r["status"] == "SKIP")

    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "python": sys.version.split()[0],
        "total": len(records),
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "exit_code": 0 if (result.wasSuccessful() and failed == 0) else 1,
        "elapsed_seconds": round(elapsed, 1),
        "tests": records,
    }
    if args.report:
        os.makedirs(os.path.dirname(os.path.abspath(args.report)), exist_ok=True)
        with open(args.report, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
            fh.write("\n")

    print("=" * 72)
    print("测试结果 / TEST RESULT")
    print("  总用例 : %d" % report["total"])
    print("  通过   : %d" % passed)
    print("  失败   : %d" % failed)
    print("  跳过   : %d" % skipped)
    print("  耗时   : %.1fs" % elapsed)
    print("  结论   : %s" % ("PASS" if report["exit_code"] == 0 else "FAIL"))
    print("  报告   : %s" % args.report)
    for r in records:
        if r["status"] in ("FAIL", "ERROR"):
            print("  [%s] %s" % (r["status"], r["test"]))
    print("=" * 72)

    if args.clean:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)
    return report["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
