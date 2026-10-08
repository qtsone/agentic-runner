"""ADR-0011 §9, the invariant the verb seams rest on.

*The Agent Runtime subprocess holds no org credential.* Push, PR, review and merge are
worker activities using the GitHub App token through an activity-side askpass shim; the
CLI never sees it. That is what makes four seams sufficient — if the subprocess held a
token, an Agent could push straight past every one of them.

Both runtimes build their child environment from an allow-list, so the test asserts the
allow-list holds against a *polluted* parent environment: a worker pod that happens to
carry GITHUB_TOKEN must not leak it into the Directive.

PRD issue 45 amends the allow-list by exactly two names — the attempt's callback socket
and its bearer — and by whatever the `environment` Runner Hook exported. Both arrive on
``DirectiveRequest.extra_env``, and the second half of this file pins that the amendment
is those two names and the hook's own non-reserved exports, and nothing else.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from agentic_runner.callback import CALLBACK_SOCKET_ENV, CALLBACK_TOKEN_ENV
from agentic_runner.hooks import _read_env_file
from agentic_runner.llm_proxy import attempt_env
from agentic_runner.workers._runtime_support import SubprocessResult
from agentic_runner.workers.agent_runtime import DirectiveRequest
from agentic_runner.workers.claude_runtime import ClaudeRuntime
from agentic_runner.workers.codex_runtime import CodexRuntime
from agentic_runner.workers.settings import WorkerSettings

# Every name by which a git or GitHub credential reaches a child process. A runtime whose
# env carries any of these has broken the invariant, whatever else it does right.
CREDENTIAL_ENV_DENYLIST = (
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "GITHUB_APP_PRIVATE_KEY",
    "GITHUB_APP_ID",
    "GITHUB_INSTALLATION_ID",
    "GH_CONFIG_DIR",
    "GIT_ASKPASS",
    "SSH_ASKPASS",
    "GIT_CONFIG",
    "GIT_CONFIG_GLOBAL",
    "GIT_TERMINAL_PROMPT",
    "SSH_AUTH_SOCK",
    "INTERNAL_SERVICE_TOKEN",
    "DATABASE_URL",
)


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


def _request(workspace: Path) -> DirectiveRequest:
    return DirectiveRequest(
        workspace_path=workspace,
        prompt="do the work",
        base_branch="main",
        work_branch="agent/work",
    )


class _EnvCapturingRunner:
    def __init__(self) -> None:
        self.env: dict[str, str] = {}
        self.argv: list[str] = []

    async def __call__(self, **kwargs: Any) -> SubprocessResult:
        self.env = dict(kwargs["env"])
        self.argv = list(kwargs["argv"])
        return SubprocessResult(exit_code=0, stdout="", stderr="")


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime_class", (CodexRuntime, ClaudeRuntime))
async def test_the_directive_subprocess_carries_no_git_or_github_credential(
    runtime_class: type[CodexRuntime] | type[ClaudeRuntime],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in CREDENTIAL_ENV_DENYLIST:
        monkeypatch.setenv(name, f"leaked-{name}")
    workspace = tmp_path / "workspaces" / "repo"
    workspace.mkdir(parents=True)
    runner = _EnvCapturingRunner()

    await runtime_class(settings=_settings(tmp_path), runner=runner).execute_directive(
        _request(workspace)
    )

    assert runner.env, "the runtime must build an explicit child environment, not inherit one"
    leaked = sorted(set(runner.env) & set(CREDENTIAL_ENV_DENYLIST))
    assert leaked == []
    assert not any(value.startswith("leaked-") for value in runner.env.values())


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime_class", (CodexRuntime, ClaudeRuntime))
async def test_the_attempt_env_gains_the_callback_pair_and_the_hooks_safe_exports(
    runtime_class: type[CodexRuntime] | type[ClaudeRuntime],
    tmp_path: Path,
) -> None:
    """Issue 09's allow-list, amended by issue 45 — and by nothing else.

    A reserved name offered through the same channel is dropped, because that is how an
    `environment` hook would otherwise move the Contract's harness config root somewhere
    another Contract can read (ADR-0015 §4).
    """

    workspace = tmp_path / "workspaces" / "repo"
    workspace.mkdir(parents=True)
    settings = _settings(tmp_path)
    baseline = _EnvCapturingRunner()
    await runtime_class(settings=settings, runner=baseline).execute_directive(_request(workspace))
    amended = _EnvCapturingRunner()

    await runtime_class(settings=settings, runner=amended).execute_directive(
        replace(
            _request(workspace),
            extra_env=(
                (CALLBACK_SOCKET_ENV, "/run/agentic-runner/abc/s.sock"),
                (CALLBACK_TOKEN_ENV, "attempt-bearer"),
                ("BUILD_FLAVOUR", "fast"),
                ("CODEX_HOME", "/tmp/hijacked"),
                ("ANTHROPIC_API_KEY", "sk-ant-hijacked"),
            ),
        )
    )

    assert set(amended.env) - set(baseline.env) == {
        CALLBACK_SOCKET_ENV,
        CALLBACK_TOKEN_ENV,
        "BUILD_FLAVOUR",
    }
    assert "hijacked" not in "".join(amended.env.values())


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime_class", (CodexRuntime, ClaudeRuntime))
async def test_the_llm_proxy_replaces_the_provider_key_it_does_not_sit_beside_it(
    runtime_class: type[CodexRuntime] | type[ClaudeRuntime],
    tmp_path: Path,
) -> None:
    """PRD issue 43: the subprocess gets the proxy's URL and the attempt's bearer.

    The point of relocating the proxy is that the Directive holds no provider key at all.
    A key left in the environment beside the proxy endpoint would be a way straight past
    the metering point, charged to the funder and recorded nowhere.
    """

    workspace = tmp_path / "workspaces" / "repo"
    workspace.mkdir(parents=True)
    runner = _EnvCapturingRunner()

    await runtime_class(settings=_settings(tmp_path), runner=runner).execute_directive(
        replace(
            _request(workspace),
            extra_env=tuple(
                attempt_env(
                    "claude_code" if runtime_class is ClaudeRuntime else "codex_cli",
                    base_url="http://127.0.0.1:9/a/attempt",
                    token="attempt-bearer",
                ).items()
            ),
        )
    )

    assert "ANTHROPIC_API_KEY" not in runner.env
    assert "sk-ant-test-key" not in "".join(runner.env.values())
    assert runner.env.get("ANTHROPIC_BASE_URL", runner.env.get("OPENAI_BASE_URL", "")).startswith(
        "http://127.0.0.1:9/a/attempt"
    )


def test_a_hook_may_not_point_the_agent_at_an_endpoint_of_its_own(tmp_path: Path) -> None:
    """The proxy endpoint is reserved for the same reason CODEX_HOME is: an `environment`
    hook that could set it would route the Agent's traffic past the metering point while
    the funder still paid for it (PRD issue 43).

    Asserted where the rule lives — the hook's exports are filtered as they are read, so
    the Runner's own proxy pair can still travel the same channel afterwards.
    """

    env_file = tmp_path / "environment.env"
    env_file.write_text(
        "ANTHROPIC_BASE_URL=http://attacker.test/v1\n"
        "OPENAI_BASE_URL=http://attacker.test/v1\n"
        "ANTHROPIC_AUTH_TOKEN=attacker\n"
        "OPENAI_API_KEY=attacker\n"
        "BUILD_FLAVOUR=fast\n",
        encoding="utf-8",
    )

    assert _read_env_file(env_file) == {"BUILD_FLAVOUR": "fast"}
