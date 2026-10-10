#!/usr/bin/env python3
"""LLM boundary: structured, validated, traceable, and optional.

The design rule (from the brief) is that the model only ever does probabilistic
work, and its output must be structured + validated + traceable:

    chapter text --> LLM structured extraction --> JSON Canon Delta
                 --> validator --> human-readable summary --> database

This module is the *transport* half of that contract. It knows how to obtain a
JSON document from an external model process and nothing else:

  provider = none      no model available; callers MUST fall back to rule extraction
  provider = rule      deterministic extractor only (default; fully offline)
  provider = command   run an external command, feed the prompt on stdin,
                       read one JSON object from stdout
  provider = file:<p>  read a pre-produced JSON document (agent-authored delta)

Every failure mode (missing command, timeout, non-JSON output, missing keys) is
reported as a NarrativeError so the caller can record a warning and fall back.
Canon is never written from an unvalidated model response.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _common import NarrativeError, read_text  # noqa: E402

PROVIDERS = ("none", "rule", "command", "file")

DEFAULT_TIMEOUT = 300


def parse_provider(spec: str) -> tuple:
    """'file:/tmp/d.json' -> ('file', '/tmp/d.json'); 'command:llm --json' -> ..."""
    if not spec:
        return "none", None
    if ":" in spec:
        name, _, arg = spec.partition(":")
        return name.strip(), arg.strip() or None
    return spec.strip(), None


def describe(spec: str) -> dict:
    name, arg = parse_provider(spec)
    if name not in PROVIDERS:
        raise NarrativeError("unknown llm provider %r (use %s)"
                            % (name, "|".join(PROVIDERS)))
    return {"provider": name, "argument": arg, "usable": name != "none"}


def extract_json_block(text: str):
    """Pull the first JSON object out of a model response.

    Models wrap JSON in prose or fences no matter what the prompt says, so this
    tolerates ```json fences and leading/trailing chatter instead of failing.
    """
    if text is None:
        raise NarrativeError("empty model response")
    s = text.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s
        if "```" in s:
            s = s.rsplit("```", 1)[0]
    s = s.strip()
    try:
        return json.loads(s)
    except ValueError:
        pass
    start = s.find("{")
    if start < 0:
        raise NarrativeError("model response contains no JSON object")
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(s[start:i + 1])
                except ValueError as exc:
                    raise NarrativeError("model JSON is malformed: %s" % exc)
    raise NarrativeError("model response JSON is truncated")


def run_json(spec: str, prompt: str, *, timeout: int = DEFAULT_TIMEOUT) -> dict:
    """Obtain one JSON document from the configured provider."""
    name, arg = parse_provider(spec)
    if name == "none":
        raise NarrativeError("llm provider is 'none'")
    if name == "rule":
        raise NarrativeError("llm provider 'rule' has no model; use the rule extractor")
    if name == "file":
        if not arg or not os.path.isfile(arg):
            raise NarrativeError("llm file provider: no such file: %r" % arg)
        try:
            return json.loads(read_text(arg))
        except ValueError as exc:
            raise NarrativeError("llm file provider: invalid JSON in %s: %s" % (arg, exc))
    if name != "command":
        raise NarrativeError("unknown llm provider %r" % name)
    if not arg:
        raise NarrativeError("llm command provider needs a command")
    cmd = shlex.split(arg, posix=(os.name != "nt"))
    try:
        proc = subprocess.run(cmd, input=prompt.encode("utf-8"),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=timeout)
    except FileNotFoundError as exc:
        raise NarrativeError("llm command not found: %s" % exc)
    except subprocess.TimeoutExpired:
        raise NarrativeError("llm command timed out after %ss" % timeout)
    if proc.returncode != 0:
        raise NarrativeError("llm command exited %d: %s"
                            % (proc.returncode, proc.stderr.decode("utf-8", "replace")[:400]))
    return extract_json_block(proc.stdout.decode("utf-8", "replace"))
