"""Credential References on the Runner (PRD issue 48; map 22 A1, 17 A4/A10).

The invariant, in one sentence: a resolved credential reaches a **verb seam** and a
**Runner-hosted MCP server**, and nothing else on the box. The two processes that must
never see one are the Agent Runtime subprocess (ADR-0011 §9) and a Runner Hook (17 A10),
and both take an environment mapping -- so the denylist below is asserted against the
environment each of them is actually built with, against a value we know was resolved.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agentic_runner.activities import RunnerRalphActivities, _RuntimeContextState
from agentic_runner.config import RunnerConfig
from agentic_runner.credentials import (
    CREDENTIAL_UNRESOLVABLE_SOURCE,
    CredentialResolver,
    DirectiveCredentials,
    DirectoryCredentialStore,
    EmptyCredentialStore,
    FakeCredentialStore,
    UnresolvableCredentialReferenceError,
)
from agentic_runner.hooks import DIRECTIVE_HOOK_ORDER, AttemptFacts, build_hook_env
from agentic_runner.service import build_credential_resolver
from agentic_runner.workers._runtime_support import SubprocessResult
from agentic_runner.workers.agent_runtime import AuthMode, DirectiveRequest
from agentic_runner.workers.claude_runtime import ClaudeRuntime
from agentic_runner.workers.codex_runtime import CodexRuntime
from agentic_runner.workers.settings import WorkerSettings

CONTRACT = "6f1d4f3a-2b58-4c7e-9a10-0d5e8c3b7f42"
REFERENCE = "acme_deploy_token"
VALUE = "ghp-never-in-an-env-anywhere"


def _resolver(**values: str) -> CredentialResolver:
    return CredentialResolver(store=FakeCredentialStore(values))


def _resolved() -> DirectiveCredentials:
    return _resolver(**{REFERENCE: VALUE}).resolve(contract_id=CONTRACT, manifest=[REFERENCE])


class _FakeVerbSeam:
    """A privileged verb seam: it spends one named reference and records what it got."""

    def __init__(self) -> None:
        self.spent: str | None = None

    def push(self, credentials: DirectiveCredentials) -> None:
        self.spent = credentials.for_verb_seam(REFERENCE)


class _FakeRunnerHostedServer:
    """An MCP server the Runner starts (issue 58): started with a named subset."""

    def __init__(self) -> None:
        self.env: dict[str, str] = {}

    def start(self, credentials: DirectiveCredentials) -> None:
        self.env = credentials.runner_hosted_server_env([REFERENCE])


class _EnvCapturingRunner:
    def __init__(self) -> None:
        self.env: dict[str, str] = {}

    async def __call__(self, **kwargs: Any) -> SubprocessResult:
        self.env = dict(kwargs["env"])
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


def test_a_reference_resolves_from_the_host_store_into_both_permitted_consumers() -> None:
    credentials = _resolved()
    seam = _FakeVerbSeam()
    server = _FakeRunnerHostedServer()

    seam.push(credentials)
    server.start(credentials)

    assert seam.spent == VALUE
    assert server.env == {REFERENCE: VALUE}
    assert credentials.references() == (REFERENCE,)


def test_a_mounted_secret_directory_is_the_host_store(tmp_path: Path) -> None:
    """The Kubernetes 0400 Secret (issue 46) and the workstation store (issue 47) shape."""

    (tmp_path / REFERENCE).write_text(f"{VALUE}\n", encoding="utf-8")
    store = DirectoryCredentialStore(tmp_path)

    assert store.get(REFERENCE) == VALUE
    # A manifest is operator-authored data; a Contract must not be able to escape its own
    # mount by naming a traversal.
    assert store.get("../other-contract/key") is None
    assert store.get("absent") is None


def test_the_composition_root_wires_the_configured_host_store(tmp_path: Path) -> None:
    """The operator surface that makes the gate satisfiable (22 A1).

    Without a store named here a Contract with a manifest would fail every Directive on
    the deployed fleet with no operator action that could satisfy it. Unset stays
    fail-closed, which is the other half of the same rule.
    """

    (tmp_path / REFERENCE).write_text(VALUE, encoding="utf-8")
    configured = build_credential_resolver(RunnerConfig(credential_store=tmp_path))

    assert (
        configured.resolve(contract_id=CONTRACT, manifest=[REFERENCE]).for_verb_seam(REFERENCE)
        == VALUE
    )

    with pytest.raises(UnresolvableCredentialReferenceError) as refusal:
        build_credential_resolver(RunnerConfig()).resolve(
            contract_id=CONTRACT, manifest=[REFERENCE]
        )
    assert refusal.value.reason == "not_installed"


def test_a_reference_outside_the_manifest_is_never_resolvable() -> None:
    """17's credential question, answered structurally: the manifest bounds the lookup.

    The host store holds a value under this name. The Contract never declared it, so the
    Directive cannot have it -- resolution is over the manifest, and a name absent from
    it is never looked up at all.
    """

    credentials = _resolver(**{REFERENCE: VALUE, "another_contracts_key": "not-yours"}).resolve(
        contract_id=CONTRACT, manifest=[REFERENCE]
    )

    with pytest.raises(UnresolvableCredentialReferenceError) as refusal:
        credentials.for_verb_seam("another_contracts_key")
    assert refusal.value.reference == "another_contracts_key"
    assert refusal.value.reason == "not_in_manifest"


def test_an_unknown_reference_fails_closed_with_evidence_naming_it() -> None:
    with pytest.raises(UnresolvableCredentialReferenceError) as refusal:
        _resolver().resolve(contract_id=CONTRACT, manifest=[REFERENCE])

    assert refusal.value.evidence() == {
        "event": "credential_reference.unresolvable",
        "reference": REFERENCE,
        "contract_id": CONTRACT,
        "reason": "not_installed",
    }
    # Names, never a value -- not even a digest of one.
    assert VALUE not in str(refusal.value.evidence())


def test_a_runner_with_no_host_store_fails_closed_rather_than_running_without() -> None:
    with pytest.raises(UnresolvableCredentialReferenceError):
        CredentialResolver(store=EmptyCredentialStore()).resolve(
            contract_id=CONTRACT, manifest=[REFERENCE]
        )


def test_a_delivered_value_resolves_under_the_same_reference_name() -> None:
    """22 A2: the sealed path and hand-over are indistinguishable downstream.

    A funder who sealed a value and a host operator who installed one produce the same
    thing at the seam, which is what makes hand-over a legitimate answer rather than a
    degraded mode. Delivery wins where both exist: the funder is stating it is theirs.
    """

    resolver = _resolver(**{REFERENCE: "the-host-installed-one"})
    resolver.deliver(CONTRACT, REFERENCE, VALUE)

    resolved = resolver.resolve(contract_id=CONTRACT, manifest=[REFERENCE])
    assert resolved.for_verb_seam(REFERENCE) == VALUE

    # 22 A9: termination drops the value, and the host's own install is what is left.
    resolver.drop(CONTRACT, REFERENCE)
    assert (
        resolver.resolve(contract_id=CONTRACT, manifest=[REFERENCE]).for_verb_seam(REFERENCE)
        == "the-host-installed-one"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime_class", (CodexRuntime, ClaudeRuntime))
async def test_a_resolved_value_is_absent_from_the_agent_subprocess_env(
    runtime_class: type[CodexRuntime] | type[ClaudeRuntime],
    tmp_path: Path,
) -> None:
    """ADR-0011 §9, with a value we know was resolved for this Directive.

    ``tests/unit/test_runner_credential_invariant.py`` pins that no *git or GitHub*
    credential leaks into the child. This is the same assertion for the class of value
    issue 48 introduces: a Credential Reference the Runner resolved a moment earlier.
    """

    credentials = _resolved()
    assert credentials.for_verb_seam(REFERENCE) == VALUE
    workspace = tmp_path / "workspaces" / "repo"
    workspace.mkdir(parents=True)
    runner = _EnvCapturingRunner()
    # Codex on its harness-root sign-in: an api_key Codex Directive with no LLM proxy endpoint
    # is refused before spawn (LA-04), which would hide what this test checks.
    auth_mode = AuthMode.SUBSCRIPTION if runtime_class is CodexRuntime else AuthMode.API_KEY

    await runtime_class(settings=_settings(tmp_path), runner=runner).execute_directive(
        DirectiveRequest(
            workspace_path=workspace,
            prompt="do the work",
            base_branch="main",
            work_branch="agent/work",
            auth_mode=auth_mode,
        )
    )

    assert runner.env, "the runtime must build an explicit child environment"
    assert REFERENCE not in runner.env
    assert VALUE not in "".join(runner.env.values())


def test_a_resolved_value_is_absent_from_every_hook_env(tmp_path: Path) -> None:
    """17 A10: a hook sees no credential value, in any slot.

    Asserted over the whole catalogue rather than one hook: the environment is built once
    for all of them, so a regression would reach every slot at once and naming a single
    hook would leave the assertion looking narrower than the rule it defends.
    """

    credentials = _resolved()
    assert credentials.for_verb_seam(REFERENCE) == VALUE
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    environments = [
        build_hook_env(
            facts=AttemptFacts(work_record_id="wr-1", directive_id="wr-1#1", contract_id=CONTRACT),
            hook=hook,
            hook_path=tmp_path / hook.value,
            workspace_path=workspace,
            sandbox=None,
        )
        for hook in DIRECTIVE_HOOK_ORDER
    ]

    assert environments
    for environment in environments:
        assert REFERENCE not in environment
        assert VALUE not in "".join(environment.values())


class _EvidenceRecordingClient:
    """Just the one method ``_resolve_credentials`` reaches for."""

    def __init__(self) -> None:
        self.appended: list[tuple[str, dict[str, Any]]] = []

    async def append_evidence(
        self, work_record_id: str, *, source: str, payload: dict[str, Any]
    ) -> None:
        self.appended.append((source, payload))


def _activities(resolver: CredentialResolver | None) -> RunnerRalphActivities:
    return RunnerRalphActivities(
        _EvidenceRecordingClient(),  # type: ignore[arg-type]
        credentials=resolver,
    )


def _state(*references: str) -> Any:
    return _RuntimeContextState(
        reviewer=None,
        repository="acme/repo",
        verifier_argv=("true",),
        work_branch="agent/work",
        completion_criteria="do the work",
        contract_id=CONTRACT,
        credential_references=references,
    )


@pytest.mark.asyncio
async def test_a_directive_resolves_its_references_and_records_the_names() -> None:
    """The Directive-time seam (22 A1), at the point the activity actually calls it."""

    activities = _activities(_resolver(**{REFERENCE: VALUE}))

    credentials = await activities._resolve_credentials("wr-1", _state(REFERENCE))

    assert credentials is not None
    assert credentials.for_verb_seam(REFERENCE) == VALUE
    client = activities._fastapi_client
    assert isinstance(client, _EvidenceRecordingClient)
    assert client.appended == []


@pytest.mark.asyncio
async def test_a_contract_declaring_no_reference_resolves_nothing_at_all() -> None:
    """The common case stays free: no manifest, no host-store read, no Evidence."""

    activities = _activities(None)

    assert await activities._resolve_credentials("wr-1", _state()) is None


@pytest.mark.asyncio
async def test_an_unresolvable_reference_fails_the_directive_closed_with_evidence() -> None:
    activities = _activities(_resolver())

    with pytest.raises(UnresolvableCredentialReferenceError) as refusal:
        await activities._resolve_credentials("wr-1", _state(REFERENCE))

    client = activities._fastapi_client
    assert isinstance(client, _EvidenceRecordingClient)
    assert client.appended == [
        (
            CREDENTIAL_UNRESOLVABLE_SOURCE,
            {
                "event": "credential_reference.unresolvable",
                "reference": REFERENCE,
                "contract_id": CONTRACT,
                "reason": "not_installed",
            },
        )
    ]
    assert refusal.value.reference == REFERENCE
