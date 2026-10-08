"""Pull one Directive's usage event out of a harness's own stdout (PRD issue 31, 17 A9).

A device-login or setup-token Directive never touches the LLM proxy, so this is the only
place the Runner can read what it spent. Pure text parsing, no I/O: the worker calls this
on the ``DirectiveResult.stdout`` it already has and posts what comes back to
``/api/runner/v1/usage/harness`` (``workers/fastapi_client.py``).
"""

from __future__ import annotations

import json
from typing import Any


def extract_codex_turn_usage(stdout: str) -> dict[str, Any] | None:
    """The last ``turn.completed`` event's ``usage`` object from ``codex exec --json``.

    JSONL, one event per line (research/29 §1.6); the *last* ``turn.completed`` is used
    because a Directive may retry a turn internally and only the final one billed.
    """

    usage: dict[str, Any] | None = None
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") == "turn.completed":
            candidate = event.get("usage")
            if isinstance(candidate, dict):
                usage = candidate
    return usage


def extract_claude_result(stdout: str) -> dict[str, Any] | None:
    """The ``result`` message from ``claude -p --output-format json|stream-json``.

    ``json`` mode prints one object; ``stream-json`` prints one JSON value per line and
    the ``result`` message is always last (research/29 §2.6) -- so trying whole-stdout
    first and falling back to the last non-empty line covers both.
    """

    stripped = stdout.strip()
    if stripped:
        try:
            whole = json.loads(stripped)
        except ValueError:
            whole = None
        if isinstance(whole, dict) and whole.get("type") == "result":
            return whole
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") == "result":
            return event
        break
    return None
