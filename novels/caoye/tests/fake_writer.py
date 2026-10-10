#!/usr/bin/env python3
"""Deterministic Writer / Extractor stubs for the auto-next and acceptance runs.

They exist so the automated pipeline
(`auto-next --writer ... --extractor ...`) can be exercised end to end without any
model or network access. Both speak the documented protocol:

    writer    : brief JSON on stdin      -> chapter prose on stdout
    extractor : extraction prompt on stdin -> Canon Delta JSON on stdout

stdin/stdout are UTF-8 **bytes**; on Windows the default code page would corrupt
the JSON brief, which is why the harness injects PYTHONIOENCODING=utf-8 and why
these stubs read `sys.stdin.buffer` explicitly.

Both stubs are stateless and deterministic: the extractor re-derives the same
chapter/location/cast the writer used from the prompt alone, so a "round trip"
needs no shared file and leaves no artefacts behind.
"""
from __future__ import annotations

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import helpers  # noqa: E402

CAST = ("李禾", "沈默")


def _read_stdin() -> str:
    data = sys.stdin.buffer.read() if hasattr(sys.stdin, "buffer") else sys.stdin.read()
    return data.decode("utf-8") if isinstance(data, bytes) else data


def _write_stdout(text: str) -> None:
    if hasattr(sys.stdout, "buffer"):
        sys.stdout.buffer.write(text.encode("utf-8"))
        sys.stdout.buffer.flush()
    else:
        sys.stdout.write(text)
        sys.stdout.flush()


def location_for(chapter: int) -> str:
    return helpers.LOCATIONS[(chapter - 1) % len(helpers.LOCATIONS)]


def write_mode() -> int:
    brief = json.loads(_read_stdin())
    chapter = int(brief["chapters_to_write"][0])
    lo, hi = brief["chars_tolerance"]
    text = helpers.chapter_text(chapter, int(lo), int(hi), location_for(chapter))
    _write_stdout(text)
    return 0


def extract_mode() -> int:
    prompt = _read_stdin()
    m = re.search(r"第\s*(\d+)\s*章正文", prompt)
    chapter = int(m.group(1)) if m else 1
    delta = helpers.delta_payload(chapter, characters=CAST,
                                 location=location_for(chapter))
    delta["source"] = "fake-extractor"
    delta["warnings"] = [{"code": "STUB_EXTRACTOR",
                          "message": "auto-next/验收用确定性 Extractor 桩（非模型）"}]
    _write_stdout(json.dumps(delta, ensure_ascii=False))
    return 0


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "write"
    return extract_mode() if mode == "extract" else write_mode()


if __name__ == "__main__":
    raise SystemExit(main())
