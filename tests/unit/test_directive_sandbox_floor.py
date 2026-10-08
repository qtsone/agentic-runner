"""The sandbox floor is applied to every Directive, in every runtime (ADR-0011 §12).

The floor sits *under* the grant model: it is the Agent Runtime Profile's, not the user's,
and no Grant can widen it. These tests pin the call site — that the subprocess is checked
before it is spawned, in both runtimes — rather than re-testing the policy itself
(``tests/unit/test_worker_command_policy.py`` does that).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agentic_runner.workers import claude_runtime, codex_runtime
from agentic_runner.workers._runtime_support import SubprocessResult
from agentic_runner.workers.agent_runtime import AuthMode, DirectiveRequest
from agentic_runner.workers.settings import WorkerSettings


class _RecordingRunner:
    def __init__(self) -> None:
        self.spawns = 0

    async def __call__(self, **kwargs: Any) -> SubprocessResult:
        self.spawns += 1
        return SubprocessResult(exit_code=0, stdout="", stderr="")


def _settings(tmp_path: Path) -> WorkerSettings:
    return WorkerSettings(
        TEMPORAL_ADDRESS="127.0.0.1:7233",
        INTERNAL_FASTAPI_BASE_URL="http://agentic-api.internal:8000",
        WORKSPACE_ROOT=tmp_path / "workspaces",
        CODEX_HOME=tmp_path / "codex-home",
        CODEX_SANDBOX_MODE="danger-full-access",
        CODEX_ASK_FOR_APPROVAL="never",
        CODEX_STRICT_CONFIG=True,
        CODEX_POLICY_HOOK_CONFIGURED=True,
        ANTHROPIC_API_KEY="sk-ant-test-key",
        CLAUDE_DANGEROUSLY_SKIP_PERMISSIONS=True,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("module", "runtime_class", "argv_builder", "program"),
    [
        (codex_runtime, codex_runtime.CodexRuntime, "_codex_argv", "codex"),
        (claude_runtime, claude_runtime.ClaudeRuntime, "_claude_argv", "claude"),
    ],
)
async def test_a_directive_whose_argv_breaks_the_floor_is_refused_before_it_spawns(
    module: Any,
    runtime_class: Any,
    argv_builder: str,
    program: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspaces" / "repo"
    workspace.mkdir(parents=True)
    runner = _RecordingRunner()
    monkeypatch.setattr(
        module,
        argv_builder,
        lambda settings, **_: [program, "exec", "--config", "api_key=sk-live-1234"],
    )

    result = await runtime_class(settings=_settings(tmp_path), runner=runner).execute_directive(
        DirectiveRequest(
            workspace_path=workspace,
            prompt="do the work",
            base_branch="main",
            work_branch="agent/work",
            # The api_key refusal for a Codex Directive with no proxy endpoint comes first;
            # this test is about the floor (LA-04).
            auth_mode=AuthMode.SUBSCRIPTION if program == "codex" else AuthMode.API_KEY,
        )
    )

    assert runner.spawns == 0
    # Exit 126 + a "refused:" guard mode is the discriminator the Ralph Loop already
    # branches on (issue 05), so a floor refusal ends the loop with an attributable
    # Incident instead of retrying an opaque failure.
    assert result.exit_code == 126
    assert result.evidence.guard_mode.startswith("refused: command policy")
    assert "secret-looking argument is denied" in result.evidence.guard_mode
