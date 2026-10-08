"""Contract test for the AgentRuntime port (issue 10).

Every Agent Runtime that plugs into the Ralph Loop must honour the same single-turn
``execute_directive`` contract, regardless of its internals (Codex CLI, Claude Code, ...).
These tests assert only port semantics; each runtime registers a ``RuntimeContractCase``
describing how to drive it. Codex is the first case here; issue 11 appends Claude Code.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from agentic_runner.workers.agent_runtime import (
    AgentRuntime,
    AuthMode,
    DirectiveEvidence,
    DirectiveRequest,
    DirectiveResult,
)
from agentic_runner.workers.claude_runtime import ClaudeRuntime
from agentic_runner.workers.codex_runtime import CodexRuntime, SubprocessResult
from agentic_runner.workers.settings import WorkerSettings


@dataclass(frozen=True)
class RuntimeContractCase:
    """A runtime under test plus how to drive it through the port contract."""

    cli_kind: str
    auth_modes: frozenset[AuthMode]
    make_success: Callable[[Path], tuple[AgentRuntime, DirectiveRequest]]
    make_refusal: Callable[[Path], tuple[AgentRuntime, DirectiveRequest]]
    make_outside_root: Callable[[Path], tuple[AgentRuntime, DirectiveRequest]]


def _async_runner(result: SubprocessResult) -> Callable[..., Awaitable[SubprocessResult]]:
    async def _runner(**_kwargs: object) -> SubprocessResult:
        return result

    return _runner


def _codex_settings(tmp_path: Path, *, guarded: bool) -> WorkerSettings:
    guard = (
        {
            "CODEX_SANDBOX_MODE": "workspace-write",
            "CODEX_ASK_FOR_APPROVAL": "never",
            "CODEX_STRICT_CONFIG": True,
            "CODEX_POLICY_HOOK_CONFIGURED": True,
        }
        if guarded
        else {}
    )
    return WorkerSettings(
        TEMPORAL_ADDRESS="127.0.0.1:7233",
        INTERNAL_FASTAPI_BASE_URL="http://agentic-api.internal:8000",
        WORKSPACE_ROOT=tmp_path / "workspaces",
        CODEX_HOME=tmp_path / "codex-home",
        **guard,
    )


def _directive_request(workspace: Path) -> DirectiveRequest:
    return DirectiveRequest(
        workspace_path=workspace,
        prompt="apply the directive",
        base_branch="main",
        work_branch="work/contract",
    )


def _codex_success(tmp_path: Path) -> tuple[AgentRuntime, DirectiveRequest]:
    settings = _codex_settings(tmp_path, guarded=True)
    workspace = settings.WORKSPACE_ROOT / "wr" / "repo"
    workspace.mkdir(parents=True)
    runtime = CodexRuntime(
        settings=settings,
        runner=_async_runner(SubprocessResult(exit_code=0, stdout="done", stderr="")),
    )
    # Its device-login path: an `api_key` Directive with no proxy endpoint is refused.
    return runtime, replace(_directive_request(workspace), auth_mode=AuthMode.SUBSCRIPTION)


def _codex_refusal(tmp_path: Path) -> tuple[AgentRuntime, DirectiveRequest]:
    settings = _codex_settings(tmp_path, guarded=False)
    workspace = settings.WORKSPACE_ROOT / "wr" / "repo"
    workspace.mkdir(parents=True)
    runtime = CodexRuntime(
        settings=settings,
        runner=_async_runner(SubprocessResult(exit_code=0, stdout="", stderr="")),
    )
    return runtime, _directive_request(workspace)


def _codex_outside_root(tmp_path: Path) -> tuple[AgentRuntime, DirectiveRequest]:
    settings = _codex_settings(tmp_path, guarded=True)
    runtime = CodexRuntime(
        settings=settings,
        runner=_async_runner(SubprocessResult(exit_code=0, stdout="", stderr="")),
    )
    outside = tmp_path / "outside" / "repo"
    outside.mkdir(parents=True)
    return runtime, _directive_request(outside)


def _claude_settings(tmp_path: Path, *, guarded: bool) -> WorkerSettings:
    guard = (
        {
            "ANTHROPIC_API_KEY": "sk-ant-contract-key",
            "CLAUDE_DANGEROUSLY_SKIP_PERMISSIONS": True,
        }
        if guarded
        else {}
    )
    return WorkerSettings(
        TEMPORAL_ADDRESS="127.0.0.1:7233",
        INTERNAL_FASTAPI_BASE_URL="http://agentic-api.internal:8000",
        WORKSPACE_ROOT=tmp_path / "workspaces",
        CODEX_HOME=tmp_path / "codex-home",
        **guard,
    )


def _claude_success(tmp_path: Path) -> tuple[AgentRuntime, DirectiveRequest]:
    settings = _claude_settings(tmp_path, guarded=True)
    workspace = settings.WORKSPACE_ROOT / "wr" / "repo"
    workspace.mkdir(parents=True)
    runtime = ClaudeRuntime(
        settings=settings,
        runner=_async_runner(SubprocessResult(exit_code=0, stdout="done", stderr="")),
    )
    return runtime, _directive_request(workspace)


def _claude_refusal(tmp_path: Path) -> tuple[AgentRuntime, DirectiveRequest]:
    settings = _claude_settings(tmp_path, guarded=False)
    workspace = settings.WORKSPACE_ROOT / "wr" / "repo"
    workspace.mkdir(parents=True)
    runtime = ClaudeRuntime(
        settings=settings,
        runner=_async_runner(SubprocessResult(exit_code=0, stdout="", stderr="")),
    )
    return runtime, _directive_request(workspace)


def _claude_outside_root(tmp_path: Path) -> tuple[AgentRuntime, DirectiveRequest]:
    settings = _claude_settings(tmp_path, guarded=True)
    runtime = ClaudeRuntime(
        settings=settings,
        runner=_async_runner(SubprocessResult(exit_code=0, stdout="", stderr="")),
    )
    outside = tmp_path / "outside" / "repo"
    outside.mkdir(parents=True)
    return runtime, _directive_request(outside)


RUNTIME_CASES: list[RuntimeContractCase] = [
    RuntimeContractCase(
        cli_kind="codex_cli",
        auth_modes=frozenset({AuthMode.API_KEY, AuthMode.SUBSCRIPTION}),
        make_success=_codex_success,
        make_refusal=_codex_refusal,
        make_outside_root=_codex_outside_root,
    ),
    RuntimeContractCase(
        cli_kind="claude_code",
        auth_modes=frozenset({AuthMode.API_KEY}),
        make_success=_claude_success,
        make_refusal=_claude_refusal,
        make_outside_root=_claude_outside_root,
    ),
]

_case = pytest.mark.parametrize("case", RUNTIME_CASES, ids=lambda case: case.cli_kind)


@_case
def test_runtime_satisfies_agent_runtime_port(case: RuntimeContractCase, tmp_path: Path) -> None:
    runtime, _ = case.make_success(tmp_path)
    assert isinstance(runtime, AgentRuntime)
    assert runtime.auth_modes == case.auth_modes


@_case
@pytest.mark.asyncio
async def test_execute_directive_returns_directive_result_on_success(
    case: RuntimeContractCase, tmp_path: Path
) -> None:
    runtime, request = case.make_success(tmp_path)

    result = await runtime.execute_directive(request)

    assert isinstance(result, DirectiveResult)
    assert result.exit_code == 0
    assert isinstance(result.evidence, DirectiveEvidence)
    assert result.command_hash
    assert not result.evidence.guard_mode.startswith("refused")


@_case
@pytest.mark.asyncio
async def test_execute_directive_marks_guard_refusal(
    case: RuntimeContractCase, tmp_path: Path
) -> None:
    runtime, request = case.make_refusal(tmp_path)

    result = await runtime.execute_directive(request)

    assert result.exit_code == 126
    assert result.evidence.guard_mode.startswith("refused")


@_case
@pytest.mark.asyncio
async def test_execute_directive_rejects_workspace_outside_root(
    case: RuntimeContractCase, tmp_path: Path
) -> None:
    runtime, request = case.make_outside_root(tmp_path)

    with pytest.raises(ValueError):
        await runtime.execute_directive(request)
