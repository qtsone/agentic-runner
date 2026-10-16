from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from agentic_runner.llm_proxy import attempt_env
from agentic_runner.service import build_agent_runtimes
from agentic_runner.workers._runtime_support import SubprocessResult
from agentic_runner.workers.agent_runtime import AuthMode, DirectiveRequest
from agentic_runner.workers.claude_runtime import ClaudeRuntime
from agentic_runner.workers.codex_runtime import CodexRuntime
from agentic_runner.workers.contract_isolation import DirectiveSandbox
from agentic_runner.workers.settings import WorkerSettings
from agentic_runner_contracts.runner_registration import CliVersion


@dataclass
class RunnerCall:
    argv: list[str]
    cwd: Path
    env: dict[str, str]
    stdin: str | None
    timeout_seconds: int
    output_limit_bytes: int


class FakeRunner:
    def __init__(self, result: SubprocessResult) -> None:
        self.result = result
        self.calls: list[RunnerCall] = []

    async def __call__(
        self,
        *,
        argv: list[str],
        cwd: Path,
        env: dict[str, str],
        stdin: str | None,
        timeout_seconds: int,
        output_limit_bytes: int,
        sandbox: object = None,
    ) -> SubprocessResult:
        self.calls.append(
            RunnerCall(
                argv=argv,
                cwd=cwd,
                env=env,
                stdin=stdin,
                timeout_seconds=timeout_seconds,
                output_limit_bytes=output_limit_bytes,
            )
        )
        return self.result


PROXY_ENV = attempt_env("claude_code", base_url="http://127.0.0.1:9/a/attempt", token="bearer")


def _settings(tmp_path: Path, **overrides: object) -> WorkerSettings:
    base: dict[str, object] = {
        "TEMPORAL_ADDRESS": "127.0.0.1:7233",
        "INTERNAL_FASTAPI_BASE_URL": "http://agentic-api.internal:8000",
        "WORKSPACE_ROOT": tmp_path / "workspaces",
        "CODEX_HOME": tmp_path / "codex-home",
        "CLAUDE_CLI_TIMEOUT_SECONDS": 19,
        "CLAUDE_CLI_OUTPUT_LIMIT_BYTES": 120,
    }
    base.update(overrides)
    return WorkerSettings(**base)


def _enabled_settings(tmp_path: Path, **overrides: object) -> WorkerSettings:
    enabled: dict[str, object] = {
        "CLAUDE_MODEL": "claude-opus-4-8",
        "CLAUDE_DANGEROUSLY_SKIP_PERMISSIONS": True,
    }
    enabled.update(overrides)
    return _settings(tmp_path, **enabled)


def _request(workspace: Path) -> DirectiveRequest:
    return DirectiveRequest(
        workspace_path=workspace,
        prompt="Apply the fix",
        base_branch="main",
        work_branch="wr/claude",
        extra_env=tuple(PROXY_ENV.items()),
    )


@pytest.mark.asyncio
async def test_execute_directive_refuses_without_permission(tmp_path: Path) -> None:
    settings = _settings(tmp_path)  # permissions not granted
    workspace = settings.WORKSPACE_ROOT / "wr" / "repo"
    workspace.mkdir(parents=True)
    runner = FakeRunner(SubprocessResult(exit_code=0, stdout="ok", stderr=""))
    runtime = ClaudeRuntime(settings=settings, runner=runner)

    result = await runtime.execute_directive(_request(workspace))

    assert result.exit_code == 126
    assert runner.calls == []
    assert result.evidence.guard_mode.startswith("refused")
    assert "autonomous permission" in result.error


@pytest.mark.asyncio
async def test_execute_directive_invokes_claude_print_bare_in_workspace(tmp_path: Path) -> None:
    settings = _enabled_settings(tmp_path)
    workspace = settings.WORKSPACE_ROOT / "wr" / "repo"
    workspace.mkdir(parents=True)
    runner = FakeRunner(SubprocessResult(exit_code=0, stdout="done", stderr=""))
    runtime = ClaudeRuntime(settings=settings, runner=runner)

    result = await runtime.execute_directive(_request(workspace))

    assert len(runner.calls) == 1
    call = runner.calls[0]
    assert call.argv[:2] == ["claude", "--print"]
    assert "--bare" in call.argv
    assert call.argv[call.argv.index("--output-format") + 1] == "json"
    assert call.argv[call.argv.index("--model") + 1] == "claude-opus-4-8"
    assert "--dangerously-skip-permissions" in call.argv
    assert all("Apply the fix" not in argument for argument in call.argv)
    assert call.cwd == workspace
    assert call.env == {"PATH": os.environ["PATH"], **PROXY_ENV}
    assert call.stdin == "Apply the fix"
    assert call.timeout_seconds == settings.CLAUDE_CLI_TIMEOUT_SECONDS
    assert call.output_limit_bytes == settings.CLAUDE_CLI_OUTPUT_LIMIT_BYTES
    assert result.exit_code == 0
    assert result.command_hash
    assert result.evidence.workspace_id == "wr/repo"
    assert result.evidence.guard_mode == "auth=api-key; permission=skip-in-sandbox; output=json"
    # No model on the Agent: the Runner's own default, and the result says which ran.
    assert result.model == "claude-opus-4-8"


@pytest.mark.asyncio
async def test_execute_directive_runs_the_agents_model_over_the_runner_default(
    tmp_path: Path,
) -> None:
    settings = _enabled_settings(tmp_path)
    workspace = settings.WORKSPACE_ROOT / "wr" / "repo"
    workspace.mkdir(parents=True)
    runner = FakeRunner(SubprocessResult(exit_code=0, stdout="done", stderr=""))
    runtime = ClaudeRuntime(settings=settings, runner=runner)

    result = await runtime.execute_directive(
        replace(_request(workspace), model="claude-sonnet-5-5")
    )

    (call,) = runner.calls
    assert call.argv[call.argv.index("--model") + 1] == "claude-sonnet-5-5"
    assert "claude-opus-4-8" not in call.argv
    assert result.model == "claude-sonnet-5-5"


@pytest.mark.asyncio
async def test_execute_directive_redacts_api_key_and_secrets(tmp_path: Path) -> None:
    settings = _enabled_settings(tmp_path)
    workspace = settings.WORKSPACE_ROOT / "wr" / "repo"
    workspace.mkdir(parents=True)
    leaky_stdout = (
        "ANTHROPIC_API_KEY=sk-ant-supersecret-9999 calling api "
        '{"token":"json-secret"} /home/worker/.claude/config.json'
    )
    runner = FakeRunner(SubprocessResult(exit_code=1, stdout=leaky_stdout, stderr=""))
    runtime = ClaudeRuntime(settings=settings, runner=runner)

    result = await runtime.execute_directive(_request(workspace))

    rendered = result.stdout + result.stderr + result.error + " ".join(result.evidence.notes)
    assert "sk-ant-supersecret-9999" not in rendered
    assert "json-secret" not in rendered
    assert "/home/worker/.claude/config.json" not in rendered
    assert "[REDACTED]" in rendered


@pytest.mark.asyncio
async def test_execute_directive_rejects_workspace_outside_worker_root(tmp_path: Path) -> None:
    settings = _enabled_settings(tmp_path)
    runner = FakeRunner(SubprocessResult(exit_code=0, stdout="", stderr=""))
    runtime = ClaudeRuntime(settings=settings, runner=runner)

    with pytest.raises(ValueError, match="workspace_path must be under WORKSPACE_ROOT"):
        await runtime.execute_directive(_request(tmp_path / "outside" / "repo"))

    assert runner.calls == []


@pytest.mark.asyncio
async def test_execute_directive_returns_bounded_redacted_timeout_result(tmp_path: Path) -> None:
    settings = _enabled_settings(tmp_path)
    workspace = settings.WORKSPACE_ROOT / "wr" / "repo"
    workspace.mkdir(parents=True)

    async def timeout_runner(**_kwargs: object) -> SubprocessResult:
        raise TimeoutError("timed out with token: timeout-secret")

    runtime = ClaudeRuntime(settings=settings, runner=timeout_runner)

    result = await runtime.execute_directive(_request(workspace))

    assert result.exit_code == 124
    assert "timeout-secret" not in result.error
    assert "[REDACTED]" in result.error


@pytest.mark.asyncio
async def test_execute_directive_propagates_cancellation(tmp_path: Path) -> None:
    settings = _enabled_settings(tmp_path)
    workspace = settings.WORKSPACE_ROOT / "wr" / "repo"
    workspace.mkdir(parents=True)

    async def cancelled_runner(**_kwargs: object) -> SubprocessResult:
        raise asyncio.CancelledError

    runtime = ClaudeRuntime(settings=settings, runner=cancelled_runner)

    with pytest.raises(asyncio.CancelledError):
        await runtime.execute_directive(_request(workspace))


def _sandbox(tmp_path: Path) -> DirectiveSandbox:
    return DirectiveSandbox(
        home_dir=tmp_path / "home",
        harness_config_dir=tmp_path / "harness",
        max_processes=None,
        max_memory_bytes=None,
    )


@pytest.mark.asyncio
async def test_a_subscription_directive_runs_behind_the_sign_in_launcher_without_bare(
    tmp_path: Path,
) -> None:
    settings = _enabled_settings(tmp_path)
    workspace = settings.WORKSPACE_ROOT / "wr" / "repo"
    workspace.mkdir(parents=True)
    runner = FakeRunner(SubprocessResult(exit_code=0, stdout="done", stderr=""))
    runtime = ClaudeRuntime(settings=settings, runner=runner)
    sandbox = _sandbox(tmp_path)

    result = await runtime.execute_directive(
        replace(_request(workspace), auth_mode=AuthMode.SUBSCRIPTION, sandbox=sandbox)
    )

    (call,) = runner.calls
    launcher, claude = call.argv[:5], call.argv[5:]
    assert launcher[:2] == ["/bin/sh", "-c"]
    assert launcher[3:] == ["agentic-claude", str(sandbox.harness_config_dir)]
    assert claude[:2] == ["claude", "--print"]
    assert "--bare" not in claude
    assert claude[claude.index("--setting-sources") + 1] == ""
    # The proxy pair never reaches a subscription turn, so its bearer goes nowhere else.
    assert call.env == {
        "PATH": os.environ["PATH"],
        "CLAUDE_CONFIG_DIR": str(sandbox.harness_config_dir),
        "HOME": str(sandbox.home_dir),
        "TMPDIR": str(sandbox.tmp_dir),
    }
    assert result.exit_code == 0
    assert result.evidence.guard_mode == (
        "auth=subscription; permission=skip-in-sandbox; output=json"
    )


@pytest.mark.asyncio
async def test_a_subscription_directive_without_a_harness_root_is_refused(tmp_path: Path) -> None:
    settings = _enabled_settings(tmp_path)
    workspace = settings.WORKSPACE_ROOT / "wr" / "repo"
    workspace.mkdir(parents=True)
    runner = FakeRunner(SubprocessResult(exit_code=0, stdout="done", stderr=""))
    runtime = ClaudeRuntime(settings=settings, runner=runner)

    result = await runtime.execute_directive(
        replace(_request(workspace), auth_mode=AuthMode.SUBSCRIPTION)
    )

    assert runner.calls == []
    assert result.exit_code == 126
    assert result.evidence.guard_mode == (
        "refused: subscription mode without a Contract harness root"
    )


def _cli(cli_kind: str, *, meets_floor: bool = True) -> CliVersion:
    return CliVersion(cli_kind=cli_kind, version="1.0.0", meets_floor=meets_floor)


def test_build_agent_runtimes_serves_every_cli_found_above_its_floor(tmp_path: Path) -> None:
    # local-agents 01: one Runner, every runtime it can run, keyed by the Profile's kind.
    runtimes = build_agent_runtimes(
        _settings(tmp_path), [_cli("claude_code"), _cli("codex_cli"), _cli("mystery_runtime")]
    )

    assert set(runtimes) == {"claude_code", "codex_cli"}
    assert isinstance(runtimes["claude_code"], ClaudeRuntime)
    assert runtimes["claude_code"].auth_modes == {AuthMode.API_KEY, AuthMode.SUBSCRIPTION}
    assert isinstance(runtimes["codex_cli"], CodexRuntime)
    assert runtimes["codex_cli"].auth_modes == {AuthMode.API_KEY, AuthMode.SUBSCRIPTION}


def test_build_agent_runtimes_leaves_out_a_cli_below_its_floor(tmp_path: Path) -> None:
    runtimes = build_agent_runtimes(
        _settings(tmp_path), [_cli("claude_code", meets_floor=False), _cli("codex_cli")]
    )

    assert set(runtimes) == {"codex_cli"}
