"""Unit tests for the Codex subprocess environment builder.

Regression cover for the ``/admin/codex`` 502 incident: Codex CLI subprocesses were
spawned with ``env={"CODEX_HOME": ...}`` and no ``PATH``. Python resolves a bare
``argv[0]`` against the *child* env's ``PATH``, falling back to ``os.defpath`` =
``/bin:/usr/bin`` — but the CLI is installed at ``/usr/local/bin/codex`` (a symlink to
``codex.js`` with a ``#!/usr/bin/env node`` shebang, ``node`` also under
``/usr/local/bin``), so exec failed with ``FileNotFoundError``. The builder must hand the
child a usable ``PATH`` while keeping the parent's secret-bearing environment out.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentic_runner.workers.codex_runtime import build_codex_subprocess_env


def test_env_carries_codex_home_and_inherits_path(monkeypatch: pytest.MonkeyPatch) -> None:
    # In the pod, os.environ["PATH"] includes /usr/local/bin, so the child must inherit it
    # to resolve `codex` (and the shebang's `node`).
    monkeypatch.setenv("PATH", "/custom/bin:/usr/local/bin:/usr/bin")

    env = build_codex_subprocess_env(Path("/var/lib/agentic-os/codex"))

    assert env["CODEX_HOME"] == "/var/lib/agentic-os/codex"
    assert env["PATH"] == "/custom/bin:/usr/local/bin:/usr/bin"


def test_env_falls_back_to_install_dir_when_path_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    # A degenerate pod with no PATH must still resolve `codex` and its `node` shebang, so
    # the fallback has to include the Codex install directory (and os.defpath).
    monkeypatch.delenv("PATH", raising=False)

    env = build_codex_subprocess_env(Path("/var/lib/agentic-os/codex"))

    assert "/usr/local/bin" in env["PATH"].split(":")


def test_env_does_not_leak_parent_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    # The minimal env is deliberate: parent secrets must not reach the Codex child. Fixing
    # PATH resolution must not become "pass the whole os.environ".
    monkeypatch.setenv("PATH", "/usr/local/bin:/usr/bin")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-should-never-leak")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-should-never-leak")

    env = build_codex_subprocess_env(Path("/var/lib/agentic-os/codex"))

    assert "SLACK_BOT_TOKEN" not in env
    assert "OPENROUTER_API_KEY" not in env
    assert "xoxb-should-never-leak" not in env.values()
    assert "sk-should-never-leak" not in env.values()
