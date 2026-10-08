from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

import pytest

from agentic_runner.workers.agent_runtime import AuthMode, DirectiveRequest
from agentic_runner.workers.codex_runtime import (
    CodexRuntime,
    SubprocessResult,
)
from agentic_runner.workers.settings import WorkerSettings


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


def _settings(tmp_path: Path) -> WorkerSettings:
    return WorkerSettings(
        TEMPORAL_ADDRESS="127.0.0.1:7233",
        INTERNAL_FASTAPI_BASE_URL="http://agentic-api.internal:8000",
        WORKSPACE_ROOT=tmp_path / "workspaces",
        CODEX_HOME=tmp_path / "codex-home",
        CODEX_CLI_TIMEOUT_SECONDS=17,
        CODEX_CLI_OUTPUT_LIMIT_BYTES=120,
    )


def _enabled_settings(tmp_path: Path) -> WorkerSettings:
    return WorkerSettings(
        TEMPORAL_ADDRESS="127.0.0.1:7233",
        INTERNAL_FASTAPI_BASE_URL="http://agentic-api.internal:8000",
        WORKSPACE_ROOT=tmp_path / "workspaces",
        CODEX_HOME=tmp_path / "codex-home",
        CODEX_CLI_TIMEOUT_SECONDS=17,
        CODEX_CLI_OUTPUT_LIMIT_BYTES=120,
        CODEX_SANDBOX_MODE="workspace-write",
        CODEX_ASK_FOR_APPROVAL="never",
        CODEX_STRICT_CONFIG=True,
        CODEX_POLICY_HOOK_CONFIGURED=True,
    )


@pytest.mark.asyncio
async def test_execute_directive_refuses_by_default_without_sandbox_and_policy_hook(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    workspace = settings.WORKSPACE_ROOT / "task-23" / "repo"
    workspace.mkdir(parents=True)
    runner = FakeRunner(SubprocessResult(exit_code=0, stdout="ok", stderr=""))
    runtime = CodexRuntime(settings=settings, runner=runner)

    result = await runtime.execute_directive(
        DirectiveRequest(
            auth_mode=AuthMode.SUBSCRIPTION,
            workspace_path=workspace,
            prompt="Do not run without guards",
            base_branch="main",
            work_branch="task-23/codex-runtime",
        )
    )

    assert result.exit_code == 126
    assert "sandbox/policy guard configuration is required" in result.error
    assert runner.calls == []
    assert result.evidence.guard_mode == "refused: missing sandbox/policy configuration"


@pytest.mark.asyncio
async def test_execute_directive_invokes_codex_exec_in_workspace_without_shell(
    tmp_path: Path,
) -> None:
    settings = _enabled_settings(tmp_path)
    workspace = settings.WORKSPACE_ROOT / "task-23" / "repo"
    workspace.mkdir(parents=True)
    runner = FakeRunner(SubprocessResult(exit_code=0, stdout="ok", stderr=""))
    runtime = CodexRuntime(settings=settings, runner=runner)

    result = await runtime.execute_directive(
        DirectiveRequest(
            auth_mode=AuthMode.SUBSCRIPTION,
            workspace_path=workspace,
            prompt="Update the worker runtime safely",
            base_branch="main",
            work_branch="task-23/codex-runtime",
        )
    )

    assert len(runner.calls) == 1
    call = runner.calls[0]
    assert call.argv[:2] == ["codex", "exec"]
    # JSONL event stream (research/29 §1.6): without it, harness_usage.py's parser never
    # finds a `turn.completed` line and a device-login Directive's Usage Record is silent.
    assert "--json" in call.argv
    assert "--sandbox" in call.argv
    assert "workspace-write" in call.argv
    # `codex exec` rejects `--ask-for-approval`; the policy rides a config override.
    assert "--ask-for-approval" not in call.argv
    assert "approval_policy=never" in call.argv
    assert "--strict-config" in call.argv
    assert "--config" in call.argv
    assert "sandbox_workspace_write.network_access=false" in call.argv
    assert all("Update the worker runtime safely" not in argument for argument in call.argv)
    assert call.argv[-1] == "-"
    assert call.cwd == workspace
    assert call.env["CODEX_HOME"] == str(settings.CODEX_HOME)
    # A bare `codex` (argv[0]) must be resolvable, so the child needs a PATH — an env with
    # only CODEX_HOME made every worker Directive exit 127 (the /admin/codex PATH incident).
    # Stays minimal: nothing beyond CODEX_HOME/PATH/HOME (no parent secrets).
    assert call.env["PATH"]
    assert set(call.env) <= {"CODEX_HOME", "PATH", "HOME"}
    assert call.stdin == "Update the worker runtime safely"
    assert call.timeout_seconds == settings.CODEX_CLI_TIMEOUT_SECONDS
    assert call.output_limit_bytes == settings.CODEX_CLI_OUTPUT_LIMIT_BYTES
    assert result.exit_code == 0
    assert result.command_hash
    assert result.evidence.workspace_id == "task-23/repo"
    assert str(tmp_path) not in result.evidence.workspace_id
    assert result.evidence.guard_mode == (
        "sandbox=workspace-write; approval=never; strict-config; "
        "network=disabled; policy-hook=configured"
    )


@pytest.mark.asyncio
async def test_execute_directive_accepts_pod_isolated_full_access_posture(
    tmp_path: Path,
) -> None:
    # The worker pod denies bwrap the user namespaces Codex's own sandbox needs, so
    # production runs `danger-full-access` with the pod as the isolation boundary.
    settings = _enabled_settings(tmp_path).model_copy(
        update={"CODEX_SANDBOX_MODE": "danger-full-access"}
    )
    workspace = settings.WORKSPACE_ROOT / "task-23" / "repo"
    workspace.mkdir(parents=True)
    runner = FakeRunner(SubprocessResult(exit_code=0, stdout="ok", stderr=""))
    runtime = CodexRuntime(settings=settings, runner=runner)

    result = await runtime.execute_directive(
        DirectiveRequest(
            auth_mode=AuthMode.SUBSCRIPTION,
            workspace_path=workspace,
            prompt="Update the worker runtime safely",
            base_branch="main",
            work_branch="task-23/codex-runtime",
        )
    )

    assert len(runner.calls) == 1
    call = runner.calls[0]
    assert "danger-full-access" in call.argv
    # The workspace-write network override is meaningless without that sandbox.
    assert "sandbox_workspace_write.network_access=false" not in call.argv
    assert call.argv[-1] == "-"
    assert result.exit_code == 0
    assert result.evidence.guard_mode == (
        "sandbox=danger-full-access (pod-isolated); approval=never; strict-config; "
        "policy-hook=configured"
    )


@pytest.mark.asyncio
async def test_execute_directive_refuses_unknown_sandbox_mode(tmp_path: Path) -> None:
    settings = _enabled_settings(tmp_path).model_copy(update={"CODEX_SANDBOX_MODE": "read-only"})
    workspace = settings.WORKSPACE_ROOT / "task-23" / "repo"
    workspace.mkdir(parents=True)
    runner = FakeRunner(SubprocessResult(exit_code=0, stdout="ok", stderr=""))
    runtime = CodexRuntime(settings=settings, runner=runner)

    result = await runtime.execute_directive(
        DirectiveRequest(
            auth_mode=AuthMode.SUBSCRIPTION,
            workspace_path=workspace,
            prompt="Do not run without guards",
            base_branch="main",
            work_branch="task-23/codex-runtime",
        )
    )

    assert result.exit_code == 126
    assert runner.calls == []
    assert result.evidence.guard_mode == "refused: missing sandbox/policy configuration"


@pytest.mark.asyncio
async def test_execute_directive_env_carries_the_install_dir_on_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Worker half of the /admin/codex PATH incident: the child env must reach /usr/local/bin
    # so a bare `codex` (and its `#!/usr/bin/env node` shebang) resolve instead of exit-127.
    monkeypatch.setenv("PATH", "/usr/local/bin:/usr/bin")
    settings = _enabled_settings(tmp_path)
    workspace = settings.WORKSPACE_ROOT / "task-1" / "repo"
    workspace.mkdir(parents=True)
    runner = FakeRunner(SubprocessResult(exit_code=0, stdout="ok", stderr=""))

    await CodexRuntime(settings=settings, runner=runner).execute_directive(
        DirectiveRequest(
            auth_mode=AuthMode.SUBSCRIPTION,
            workspace_path=workspace,
            prompt="do the thing",
            base_branch="main",
            work_branch="task-1/codex",
        )
    )

    (call,) = runner.calls
    assert "/usr/local/bin" in call.env["PATH"].split(":")


@pytest.mark.asyncio
async def test_execute_directive_redacts_and_bounds_evidence(tmp_path: Path) -> None:
    settings = _enabled_settings(tmp_path)
    workspace = settings.WORKSPACE_ROOT / "task-23" / "repo"
    workspace.mkdir(parents=True)
    secret_stdout = (
        'OPENAI_API_KEY=sk-secret-token {"token":"json-secret"} TOKEN: colon-secret '
        "--password cli-secret /home/worker/.codex/auth.json "
        "-----BEGIN PRIVATE KEY-----\n" + ("x" * 300)
    )
    secret_stderr = "authorization: Bearer abc123secret refresh_token=refresh-secret\n"
    runner = FakeRunner(SubprocessResult(exit_code=1, stdout=secret_stdout, stderr=secret_stderr))
    runtime = CodexRuntime(settings=settings, runner=runner)

    result = await runtime.execute_directive(
        DirectiveRequest(
            auth_mode=AuthMode.SUBSCRIPTION,
            workspace_path=workspace,
            prompt="Do the thing",
            base_branch="main",
            work_branch="task-23/codex-runtime",
        )
    )

    rendered = result.stdout + result.stderr + result.error + " ".join(result.evidence.notes)
    assert "sk-secret-token" not in rendered
    assert "json-secret" not in rendered
    assert "colon-secret" not in rendered
    assert "cli-secret" not in rendered
    assert "abc123secret" not in rendered
    assert "refresh-secret" not in rendered
    assert "/home/worker/.codex/auth.json" not in rendered
    assert "PRIVATE KEY" not in rendered
    assert "[REDACTED]" in rendered
    assert len(result.stdout.encode()) <= settings.CODEX_CLI_OUTPUT_LIMIT_BYTES
    assert len(result.stderr.encode()) <= settings.CODEX_CLI_OUTPUT_LIMIT_BYTES


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "secret_path",
    [
        "~/.codex/auth.json",
        "/home/worker/.codex",
        "/home/worker/.codex/auth.json",
    ],
)
async def test_execute_directive_redacts_codex_config_path_forms(
    tmp_path: Path,
    secret_path: str,
) -> None:
    settings = _enabled_settings(tmp_path)
    workspace = settings.WORKSPACE_ROOT / "task-23" / "repo"
    workspace.mkdir(parents=True)
    runner = FakeRunner(SubprocessResult(exit_code=1, stdout=secret_path, stderr=""))
    runtime = CodexRuntime(settings=settings, runner=runner)

    result = await runtime.execute_directive(
        DirectiveRequest(
            auth_mode=AuthMode.SUBSCRIPTION,
            workspace_path=workspace,
            prompt="Do the thing",
            base_branch="main",
            work_branch="task-23/codex-runtime",
        )
    )

    assert ".codex" not in result.stdout
    assert "auth.json" not in result.stdout
    assert "/home/worker" not in result.stdout
    assert "[REDACTED]" in result.stdout


@pytest.mark.asyncio
async def test_execute_directive_redacts_secret_looking_branch_evidence(tmp_path: Path) -> None:
    settings = _enabled_settings(tmp_path)
    workspace = settings.WORKSPACE_ROOT / "task-23" / "repo"
    workspace.mkdir(parents=True)
    runner = FakeRunner(SubprocessResult(exit_code=0, stdout="ok", stderr=""))
    runtime = CodexRuntime(settings=settings, runner=runner)

    result = await runtime.execute_directive(
        DirectiveRequest(
            auth_mode=AuthMode.SUBSCRIPTION,
            workspace_path=workspace,
            prompt="Do the thing",
            base_branch="main-token: base-secret",
            work_branch="feature/~/.codex/auth.json--password branch-secret",
        )
    )

    rendered = result.evidence.base_branch + result.evidence.work_branch
    assert "base-secret" not in rendered
    assert "branch-secret" not in rendered
    assert ".codex" not in rendered
    assert "auth.json" not in rendered
    assert "[REDACTED]" in rendered


@pytest.mark.asyncio
async def test_execute_directive_returns_bounded_redacted_timeout_result(tmp_path: Path) -> None:
    settings = _enabled_settings(tmp_path)
    workspace = settings.WORKSPACE_ROOT / "task-23" / "repo"
    workspace.mkdir(parents=True)

    async def timeout_runner(**kwargs: object) -> SubprocessResult:
        raise TimeoutError("timed out with token: timeout-secret")

    runtime = CodexRuntime(settings=settings, runner=timeout_runner)

    result = await runtime.execute_directive(
        DirectiveRequest(
            auth_mode=AuthMode.SUBSCRIPTION,
            workspace_path=workspace,
            prompt="Do the thing",
            base_branch="main",
            work_branch="task-23/codex-runtime",
        )
    )

    assert result.exit_code == 124
    assert "timeout-secret" not in result.error
    assert "[REDACTED]" in result.error


@pytest.mark.asyncio
async def test_execute_directive_propagates_cancellation_for_worker_shutdown(
    tmp_path: Path,
) -> None:
    settings = _enabled_settings(tmp_path)
    workspace = settings.WORKSPACE_ROOT / "task-23" / "repo"
    workspace.mkdir(parents=True)

    async def cancelled_runner(**kwargs: object) -> SubprocessResult:
        raise asyncio.CancelledError

    runtime = CodexRuntime(settings=settings, runner=cancelled_runner)

    with pytest.raises(asyncio.CancelledError):
        await runtime.execute_directive(
            DirectiveRequest(
                auth_mode=AuthMode.SUBSCRIPTION,
                workspace_path=workspace,
                prompt="Cancel this",
                base_branch="main",
                work_branch="task-23/codex-runtime",
            )
        )


@pytest.mark.asyncio
async def test_execute_directive_rejects_workspace_outside_worker_root(tmp_path: Path) -> None:
    settings = _enabled_settings(tmp_path)
    runner = FakeRunner(SubprocessResult(exit_code=0, stdout="", stderr=""))
    runtime = CodexRuntime(settings=settings, runner=runner)

    with pytest.raises(ValueError, match="workspace_path must be under WORKSPACE_ROOT"):
        await runtime.execute_directive(
            DirectiveRequest(
                auth_mode=AuthMode.SUBSCRIPTION,
                workspace_path=tmp_path / "other" / "repo",
                prompt="Do not run",
                base_branch="main",
                work_branch="task-23/codex-runtime",
            )
        )

    assert runner.calls == []
