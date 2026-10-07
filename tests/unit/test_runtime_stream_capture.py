"""``run_subprocess_exec`` keeps the *tail* of an over-limit stream (PRD issue 31 blocker).

Codex's ``turn.completed`` and Claude Code's ``result`` message are always the last line
of stdout (``harness_usage.py``). A head-first read bound previously discarded exactly
that line on any Directive long enough to cross the limit, so a real, expensive Directive
never produced a harness Usage Record. These tests drive the real subprocess read, not a
fake, because the bug lived in the streaming read itself.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentic_runner.workers._runtime_support import run_subprocess_exec
from agentic_runner.workers.harness_usage import extract_codex_turn_usage


@pytest.mark.asyncio
async def test_over_limit_stdout_keeps_the_trailing_turn_completed_line(tmp_path: Path) -> None:
    limit_bytes = 4096
    filler = "x" * (limit_bytes * 2)
    usage_line = json.dumps(
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 24763,
                "cached_input_tokens": 24448,
                "output_tokens": 122,
                "reasoning_output_tokens": 0,
            },
        }
    )
    script = f"print({filler!r}); print({usage_line!r})"

    result = await run_subprocess_exec(
        argv=["python3", "-c", script],
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin"},
        stdin=None,
        timeout_seconds=10,
        output_limit_bytes=limit_bytes,
    )

    assert result.stdout_truncated is True
    assert len(result.stdout.encode()) <= limit_bytes
    usage = extract_codex_turn_usage(result.stdout)
    assert usage == {
        "input_tokens": 24763,
        "cached_input_tokens": 24448,
        "output_tokens": 122,
        "reasoning_output_tokens": 0,
    }


@pytest.mark.asyncio
async def test_under_limit_stdout_is_returned_whole(tmp_path: Path) -> None:
    result = await run_subprocess_exec(
        argv=["python3", "-c", "print('hello')"],
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin"},
        stdin=None,
        timeout_seconds=10,
        output_limit_bytes=4096,
    )

    assert result.stdout_truncated is False
    assert result.stdout == "hello\n"
