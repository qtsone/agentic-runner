"""Runner Hooks: the closed catalogue, the loader, and what a hook is allowed to see.

PRD issue 45, ADR-0013 §10, map ticket 26 §1 and 17 A10. The activity-level ordering and
the refusal semantics are exercised against the real Directive in
``tests/integration/test_runner_hook_lifecycle.py``; this file holds the invariants that are true
of a hook whoever runs it.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

import pytest

from agentic_runner.hooks import (
    DIRECTIVE_HOOK_ORDER,
    AttemptFacts,
    HookName,
    HookRunner,
    build_hook_env,
    load_hooks,
)
from agentic_runner.workers._runtime_support import SubprocessResult
from agentic_runner.workers.contract_isolation import DirectiveSandbox
from agentic_runner_contracts.public_metadata import FATAL_HOOKS, PublicMetadataError, hook_name

CONTRACT_UID = 4242

# Stands in for the Credential Reference store the Runner resolves at Directive time (PRD
# issue 48 builds the store itself; ADR-0013 §11 names it). What matters here is the rule
# it exists to serve: values reach verb seams and Runner-hosted MCP servers, never a hook
# (map ticket 17 A10). The Runner process environment is where every one of these lives
# on a real pod, so polluting it is the faithful negative test.
FAKE_CREDENTIAL_STORE = {
    "GITHUB_TOKEN": "ghp_hook_must_never_see_this",
    "GITHUB_APP_PRIVATE_KEY_PEM": "-----BEGIN PRIVATE KEY-----hook-must-never-see-this",
    "ANTHROPIC_API_KEY": "sk-ant-hook-must-never-see-this",
    "OPENAI_API_KEY": "sk-openai-hook-must-never-see-this",
    "OPENROUTER_API_KEY": "sk-or-hook-must-never-see-this",
    "INTERNAL_SERVICE_TOKEN": "internal-hook-must-never-see-this",
    "DATABASE_URL": "postgresql://hook:must-never-see-this@db/control_plane",
    "STRIPE_API_KEY": "sk_live_hook_must_never_see_this",
}


def _sandbox(tmp_path: Path, uid: int | None = CONTRACT_UID) -> DirectiveSandbox:
    return DirectiveSandbox(
        home_dir=tmp_path / "contract",
        harness_config_dir=tmp_path / "contract" / "harness" / "codex_cli",
        max_processes=64,
        max_memory_bytes=1024**3,
        uid=uid,
        gid=uid,
    )


def _facts() -> AttemptFacts:
    return AttemptFacts(
        work_record_id="123e4567-e89b-12d3-a456-426614174000",
        directive_id="123e4567-e89b-12d3-a456-426614174000:1",
        contract_id="1d0f6b8c-2e3f-4a5b-8c7d-9e0f1a2b3c4d",
        agent_id="8f14e45f-ceea-467a-9a37-1a2b3c4d5e6f",
        runtime_kind="codex_cli",
    )


class _RecordingSpawn:
    def __init__(self, exit_code: int = 0) -> None:
        self.calls: list[dict[str, Any]] = []
        self._exit_code = exit_code

    async def __call__(self, **kwargs: Any) -> SubprocessResult:
        self.calls.append(kwargs)
        return SubprocessResult(exit_code=self._exit_code, stdout="", stderr="")


def _install(hooks_path: Path, name: str, body: str = "#!/bin/sh\nexit 0\n") -> Path:
    hooks_path.mkdir(parents=True, exist_ok=True)
    hook = hooks_path / name
    hook.write_text(body)
    hook.chmod(hook.stat().st_mode | stat.S_IXUSR)
    return hook


def test_the_catalogue_is_closed() -> None:
    """Operators fill slots; nobody adds one (ADR-0013 §10)."""

    assert [name.value for name in HookName] == [
        "runner_startup",
        "runner_shutdown",
        "pre_directive",
        "environment",
        "pre_checkout",
        "checkout",
        "post_checkout",
        "pre_runtime",
        "post_runtime",
        "pre_verify",
        "post_verify",
        "pre_artifact",
        "post_artifact",
        "pre_exit",
    ]
    # The two Runner-lifecycle slots sit outside a Directive; everything else is ordered.
    assert set(DIRECTIVE_HOOK_ORDER) | {
        HookName.RUNNER_STARTUP,
        HookName.RUNNER_SHUTDOWN,
    } == set(HookName)
    # Fatal through `pre_runtime` and no further — `post_verify` in particular.
    through_runtime = DIRECTIVE_HOOK_ORDER[: DIRECTIVE_HOOK_ORDER.index(HookName.PRE_RUNTIME) + 1]
    assert set(through_runtime) == FATAL_HOOKS
    assert HookName.POST_VERIFY not in FATAL_HOOKS


@pytest.mark.parametrize("unknown", ("pre-directive", "post_command", "deploy", "pre_bootstrap"))
def test_a_file_outside_the_catalogue_refuses_to_load(tmp_path: Path, unknown: str) -> None:
    """A hook that would silently never run is worse than a Runner that will not start."""

    _install(tmp_path / "hooks", unknown)

    with pytest.raises(PublicMetadataError):
        load_hooks(tmp_path / "hooks")


def test_the_loader_keeps_catalogue_files_and_skips_projection_dotfiles(tmp_path: Path) -> None:
    hooks_path = tmp_path / "hooks"
    _install(hooks_path, "pre_directive")
    _install(hooks_path, "environment")
    # What a Kubernetes ConfigMap projection leaves beside the mounted keys.
    _install(hooks_path, "..data")

    assert set(load_hooks(hooks_path)) == {HookName.PRE_DIRECTIVE, HookName.ENVIRONMENT}
    assert hook_name("pre_directive") is HookName.PRE_DIRECTIVE


@pytest.mark.asyncio
async def test_a_hook_carries_no_credential_value_and_runs_as_the_contract_uid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Map ticket 17 A10 and ADR-0015 §1, asserted together on one spawn."""

    for name, value in FAKE_CREDENTIAL_STORE.items():
        monkeypatch.setenv(name, value)
    _install(tmp_path / "hooks", "pre_directive")
    spawn = _RecordingSpawn()
    runner = HookRunner(hooks_path=tmp_path / "hooks", spawn=spawn)
    sandbox = _sandbox(tmp_path)

    await runner.run(
        HookName.PRE_DIRECTIVE, cwd=tmp_path / "workspace", facts=_facts(), sandbox=sandbox
    )

    (call,) = spawn.calls
    env = call["env"]
    assert set(env) & set(FAKE_CREDENTIAL_STORE) == set()
    assert not any(value in env.values() for value in FAKE_CREDENTIAL_STORE.values())
    # ...and the uid the subprocess will actually be dropped to, not merely the object.
    assert call["sandbox"].spawn_kwargs()["user"] == CONTRACT_UID
    assert call["cwd"] == tmp_path / "workspace"


def test_the_hook_environment_is_platform_vocabulary_only(tmp_path: Path) -> None:
    """Ids and enums, never Directive text: a hook's environment is as displayed as a
    queue name (map ticket 07)."""

    facts = _facts()
    env = build_hook_env(
        facts=facts,
        hook=HookName.PRE_DIRECTIVE,
        hook_path=tmp_path / "hooks" / "pre_directive",
        workspace_path=tmp_path / "workspace",
        sandbox=_sandbox(tmp_path),
    )

    assert env["AGENTIC_RUNNER_WORK_RECORD_ID"] == facts.work_record_id
    assert env["AGENTIC_RUNNER_CONTRACT_ID"] == facts.contract_id
    assert env["AGENTIC_RUNNER_HOOK"] == "pre_directive"
    assert env["TMPDIR"] == str(_sandbox(tmp_path).tmp_dir)
    assert "AGENTIC_RUNNER_PERSONA" not in env, "an empty fact is omitted, not exported blank"


@pytest.mark.asyncio
async def test_a_hook_that_cannot_be_executed_fails_rather_than_crashing(tmp_path: Path) -> None:
    """The ConfigMap mounted without `defaultMode: 0755`, which is how this happens."""

    hooks_path = tmp_path / "hooks"
    hooks_path.mkdir()
    hook = hooks_path / "pre_directive"
    hook.write_text("#!/bin/sh\nexit 0\n")
    hook.chmod(0o600)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    run = await HookRunner(hooks_path=hooks_path).run(
        HookName.PRE_DIRECTIVE, cwd=workspace, facts=_facts()
    )

    assert run is not None
    assert run.exit_code == 126
    assert "could not be executed" in run.stdout


@pytest.mark.asyncio
async def test_an_empty_slot_runs_nothing(tmp_path: Path) -> None:
    spawn = _RecordingSpawn()
    runner = HookRunner(hooks=({}), spawn=spawn)

    assert await runner.run(HookName.PRE_EXIT, cwd=tmp_path, facts=_facts()) is None
    assert spawn.calls == []


@pytest.mark.asyncio
async def test_hook_stdout_is_scrubbed_before_it_becomes_evidence(tmp_path: Path) -> None:
    """Map ticket 26 §1: stdout is recorded, and scrubbed like any diagnostic (06 §4)."""

    hooks_path = tmp_path / "hooks"
    _install(
        hooks_path,
        "post_runtime",
        "#!/bin/sh\necho 'using ghp_0123456789abcdefghijklmnopqrstuvwxyz'\n",
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    run = await HookRunner(hooks_path=hooks_path).run(
        HookName.POST_RUNTIME, cwd=workspace, facts=_facts()
    )

    assert run is not None
    assert "ghp_" not in run.stdout
    assert run.evidence()["hook"] == "post_runtime"
    assert os.path.isdir(workspace)
