"""Each Directive's auth mode, through the real activity (local-agents 04).

What only the activity can show: the mode reaches the Agent Runtime on the request, a
shared Runner's refusal is non-retryable with Evidence naming the rule and spawns nothing,
the choice depends on the host party alone, and a login left in a Contract's harness root
on a shared Runner is reported once, as a count.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import httpx
import pytest
from temporalio.exceptions import ApplicationError

from agentic_runner import service
from agentic_runner.activities import (
    AGENT_RUNTIME_EVIDENCE_SOURCE,
    HARNESS_HOLD_SOURCE,
    HARNESS_LOGIN_RESIDUE_SOURCE,
    RunnerRalphActivities,
)
from agentic_runner.auth_mode import SHARED_RUNNER_SUBSCRIPTION_RULE
from agentic_runner.credentials import CredentialResolver, EmptyCredentialStore
from agentic_runner.integrations.git.fake_workspace import FakeGitWorkspace
from agentic_runner.integrations.github.fake_client import FakeGitHubClient
from agentic_runner.llm_proxy import CredentialSlot, LlmProxy, SlotStore
from agentic_runner.registration import RunnerState
from agentic_runner.sealed_box import RecipientKeyStore, SealedCredentialStream, seal
from agentic_runner.workers.agent_runtime import (
    AuthMode,
    DirectiveEvidence,
    DirectiveRequest,
    DirectiveResult,
    RuntimeCapabilities,
)
from agentic_runner.workers.contract_isolation import ContractIsolation
from agentic_runner_contracts import __version__ as contracts_version
from agentic_runner_contracts.activity_io import (
    BranchPullRequestInput,
    HarnessHold,
    HarnessHoldKind,
)
from agentic_runner_contracts.routing import DirectiveRouting, RunnerRoutingIdentity
from agentic_runner_contracts.runner_registration import FloorState, HeartbeatAck
from agentic_runner_contracts.runtime_context import work_branch_name
from agentic_runner_contracts.sealed_credential import SealedCredential, delivery_binding

WORK_RECORD_ID = "123e4567-e89b-12d3-a456-426614174000"
CONTRACT_ID = "11111111-1111-4111-8111-111111111111"
RUNNER_ID = "runner-1"


class _Client:
    def __init__(self) -> None:
        self.evidence: list[tuple[str, dict[str, Any]]] = []

    async def get_runtime_context(self, work_record_id: str) -> dict[str, Any]:
        return {
            "work_record_id": WORK_RECORD_ID,
            "contract_id": CONTRACT_ID,
            "profile_slug": "engineer",
            "cli_kind": "codex_cli",
            "repo": "qts/agentic-os",
            "base_branch": "main",
            "reviewer": "ralph-reviewer",
            "completion_criteria": "Add a Production section",
            "persona_slug": None,
            "persona_instructions": "",
            "task_queue": "ralph-pr-loop",
            "worker_secret_refs": [],
            "command_policy": {},
            "network_policy": {},
            "product_verifier_command_source": {
                "source": "runtime-profile",
                "available": True,
                "metadata": {"command": "python -m pytest -q"},
            },
        }

    async def get_directive_usage(self, work_record_id: str, directive_id: str) -> dict[str, Any]:
        return {}

    async def report_harness_usage(self, payload: Mapping[str, Any]) -> list[dict[str, Any]]:
        return []

    async def append_evidence(
        self,
        work_record_id: str,
        *,
        source: str,
        payload: Mapping[str, Any],
        actor: str = "temporal-worker",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        self.evidence.append((source, dict(payload)))
        return {"ok": True}

    async def transition_work_record(
        self, work_record_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return {"ok": True}

    def payloads(self, source: str) -> list[dict[str, Any]]:
        return [payload for seen, payload in self.evidence if seen == source]


@dataclass
class _Codex:
    auth_modes: frozenset[AuthMode] = frozenset({AuthMode.API_KEY, AuthMode.SUBSCRIPTION})
    host_api_key: bool = False
    requests: list[DirectiveRequest] = field(default_factory=list)

    def capabilities(self) -> RuntimeCapabilities:
        return RuntimeCapabilities(auth_modes=self.auth_modes, permission_mode="fake")

    async def execute_directive(self, request: DirectiveRequest) -> DirectiveResult:
        self.requests.append(request)
        return DirectiveResult(
            exit_code=0,
            stdout="edited files",
            stderr="",
            error="",
            command_hash="hash",
            evidence=DirectiveEvidence(
                workspace_id="qts-agentic-os",
                base_branch=request.base_branch,
                work_branch=request.work_branch,
                command_hash="hash",
                guard_mode="workspace-write/no-approval",
                notes=[],
            ),
        )


class _Proxy:
    """Holds a slot for the Contract or not; hands each attempt the proxy pair."""

    def __init__(self, *, holds: bool) -> None:
        self.slots = SimpleNamespace(holds=lambda _contract_id: holds)
        self.attempts = 0

    @contextlib.asynccontextmanager
    async def attempt(self, **_admitted: Any) -> AsyncIterator[Any]:
        self.attempts += 1
        yield SimpleNamespace(
            env=lambda _cli_kind: {
                "OPENAI_BASE_URL": "http://127.0.0.1:1/a/x/v1",
                "OPENAI_API_KEY": "attempt-bearer",
            }
        )


def _isolation(tmp_path: Path) -> ContractIsolation:
    return ContractIsolation(
        workspace_root=tmp_path,
        state_dir=tmp_path / ".state",
        uid_min=20_000,
        uid_max=20_100,
        max_processes=64,
        memory_limit_bytes=1 << 30,
        can_separate_uids=False,
    )


def _activities(
    client: _Client,
    runtime: _Codex,
    *,
    tmp_path: Path,
    host_party: str,
    tags: dict[str, str] | None = None,
    proxy: _Proxy | None = None,
    isolation: ContractIsolation | None = None,
) -> RunnerRalphActivities:
    return RunnerRalphActivities(
        client,  # type: ignore[arg-type]
        agent_runtimes={"codex_cli": runtime},
        git_workspace=FakeGitWorkspace(status_evidence=" M README.md", diff_evidence="+change"),
        github_client=FakeGitHubClient(),
        workspace_root=tmp_path,
        routing_identity=RunnerRoutingIdentity(
            runner_id=RUNNER_ID, host_party=host_party, tags=dict(tags or {})
        ),
        llm_proxy=proxy,  # type: ignore[arg-type]
        contract_isolation=isolation,
    )


def _branch_pr_input(host_party: str) -> BranchPullRequestInput:
    return BranchPullRequestInput(
        work_record_id=WORK_RECORD_ID,
        repository="qts/agentic-os",
        base_ref="main",
        branch_name=work_branch_name(work_record_id=WORK_RECORD_ID, profile_slug="engineer"),
        pr_title="Add a Production section",
        pr_body="body",
        routing=DirectiveRouting(runner_id=RUNNER_ID, host_party=host_party),
    )


@pytest.mark.parametrize("install_channel", ["helm", "workstation", "docker"])
@pytest.mark.asyncio
async def test_the_persons_own_runner_runs_a_subscription_whatever_installed_it(
    tmp_path: Path, install_channel: str
) -> None:
    # The owner's decision (2026-09-26): subscriptions follow the person, not the host.
    runtime = _Codex()

    await _activities(
        _Client(),
        runtime,
        tmp_path=tmp_path,
        host_party="user",
        tags={"install_channel": install_channel},
    ).create_or_update_branch_pr(_branch_pr_input("user"))

    [request] = runtime.requests
    assert request.auth_mode == AuthMode.SUBSCRIPTION
    assert "OPENAI_API_KEY" not in dict(request.extra_env)


@pytest.mark.asyncio
async def test_a_key_present_puts_even_the_persons_own_runner_on_the_proxy(
    tmp_path: Path,
) -> None:
    runtime = _Codex()
    proxy = _Proxy(holds=True)

    await _activities(
        _Client(), runtime, tmp_path=tmp_path, host_party="user", proxy=proxy
    ).create_or_update_branch_pr(_branch_pr_input("user"))

    [request] = runtime.requests
    assert request.auth_mode == AuthMode.API_KEY
    assert dict(request.extra_env)["OPENAI_API_KEY"] == "attempt-bearer"
    assert proxy.attempts == 1


@pytest.mark.parametrize("host_party", ["organisation", "account"])
@pytest.mark.asyncio
async def test_a_shared_runner_runs_codex_on_the_proxy_when_a_key_is_present(
    tmp_path: Path, host_party: str
) -> None:
    runtime = _Codex()

    await _activities(
        _Client(), runtime, tmp_path=tmp_path, host_party=host_party, proxy=_Proxy(holds=True)
    ).create_or_update_branch_pr(_branch_pr_input(host_party))

    [request] = runtime.requests
    assert request.auth_mode == AuthMode.API_KEY


@pytest.mark.parametrize("host_party", ["organisation", "account"])
@pytest.mark.asyncio
async def test_a_shared_runner_refuses_a_directive_that_would_need_a_subscription(
    tmp_path: Path, host_party: str
) -> None:
    client = _Client()
    runtime = _Codex()

    with pytest.raises(ApplicationError) as refused:
        await _activities(
            client, runtime, tmp_path=tmp_path, host_party=host_party, proxy=_Proxy(holds=False)
        ).create_or_update_branch_pr(_branch_pr_input(host_party))

    assert refused.value.non_retryable is True
    assert refused.value.type == SHARED_RUNNER_SUBSCRIPTION_RULE
    assert runtime.requests == [], "nothing is spawned on a refused mode"
    [evidence] = client.payloads(AGENT_RUNTIME_EVIDENCE_SOURCE)
    assert evidence["event"] == "directive.auth_mode_refused"
    assert evidence["rule"] == SHARED_RUNNER_SUBSCRIPTION_RULE
    assert evidence["host_party"] == host_party


@pytest.mark.asyncio
async def test_a_login_left_on_a_shared_runner_is_reported_once_as_a_count(
    tmp_path: Path,
) -> None:
    isolation = _isolation(tmp_path)
    login = isolation.harness_config_dir(CONTRACT_ID, "codex_cli") / "auth.json"
    login.parent.mkdir(parents=True)
    login.write_text('{"tokens": {"refresh_token": "never-read"}}')
    client = _Client()
    activities = _activities(
        client,
        _Codex(),
        tmp_path=tmp_path,
        host_party="organisation",
        proxy=_Proxy(holds=True),
        isolation=isolation,
    )

    await activities.create_or_update_branch_pr(_branch_pr_input("organisation"))
    await activities.create_or_update_branch_pr(_branch_pr_input("organisation"))

    [report] = client.payloads(HARNESS_LOGIN_RESIDUE_SOURCE)
    assert report == {
        "event": "harness.login_residue",
        "contract_id": CONTRACT_ID,
        "host_party": "organisation",
        "login_files": {"codex_cli": 1},
    }
    assert str(tmp_path) not in repr(client.evidence)
    assert "never-read" not in repr(client.evidence)
    assert login.is_file(), "the Runner reports the login; the Contract's wipe removes it"


class _Registration:
    """A control plane holding one sealed row: the funder's OpenAI key for the Contract."""

    server_date = None

    def __init__(self) -> None:
        self.rows: list[SealedCredential] = []

    async def heartbeat(self, state: Any, envelope: Any) -> HeartbeatAck:
        return HeartbeatAck(
            runner_id=uuid4(),
            floor_state=FloorState.OK,
            contracts_floor=contracts_version,
            tag_set_version=1,
            accepts_new_directives=True,
            sealed_credentials=list(self.rows),
        )


@dataclass
class _SpendingCodex(_Codex):
    """Does what Codex does in `api_key` mode: one Responses call on the attempt's pair."""

    status: int = 0

    async def execute_directive(self, request: DirectiveRequest) -> DirectiveResult:
        env = dict(request.extra_env)
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{env['OPENAI_BASE_URL']}/responses",
                json={"model": "gpt-5", "input": "go"},
                headers={"Authorization": f"Bearer {env['OPENAI_API_KEY']}"},
            )
        self.status = response.status_code
        return await super().execute_directive(request)


@pytest.mark.asyncio
async def test_a_delivered_key_puts_the_next_shared_directive_on_the_proxy(
    tmp_path: Path,
) -> None:
    # Local-agents 04b: before the delivery a shared Runner refuses this Directive (no key,
    # no subscription); after it, the same Directive runs on the funder's key, which only
    # the proxy ever holds.
    seen: list[str] = []

    def provider(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["authorization"])
        return httpx.Response(200, json={"usage": {"input_tokens": 12, "output_tokens": 3}})

    async def always_valid(_: CredentialSlot) -> bool:
        return True

    proxy = LlmProxy(
        slots=SlotStore(probe=always_valid),
        client=httpx.AsyncClient(transport=httpx.MockTransport(provider)),
    )
    registration = _Registration()
    keys = RecipientKeyStore(tmp_path / "state")
    stream = service.ControlPlaneStream(
        client=registration,  # type: ignore[arg-type]
        state=RunnerState(
            runner_id=uuid4(),
            identity_id="id",
            private_key_pem="unused",
            temporal_namespace="org-x",
            task_queue="runner.x",
        ),
        isolation=service.IsolationMode.NONE,
        proxy=proxy,
        sealed=SealedCredentialStream(keys),
        credentials=CredentialResolver(store=EmptyCredentialStore()),
        load=service._LoadInterceptor(),
    )
    key = keys.current()
    registration.rows = [
        SealedCredential(
            contract_id=UUID(CONTRACT_ID),
            slot="OPENAI_API_KEY",
            recipient_key_id=key.key_id,
            version=1,
            ciphertext=seal(
                public_key=key.public_key,
                binding=delivery_binding(
                    contract_id=CONTRACT_ID, slot="OPENAI_API_KEY", recipient_key_id=key.key_id
                ),
                plaintext="sk-funder-key",
            ),
        )
    ]
    runtime = _SpendingCodex()

    async with proxy:
        activities = _activities(
            _Client(),
            runtime,
            tmp_path=tmp_path,
            host_party="organisation",
            proxy=proxy,  # type: ignore[arg-type]
        )
        with pytest.raises(ApplicationError) as refused:
            await activities.create_or_update_branch_pr(_branch_pr_input("organisation"))
        assert refused.value.type == SHARED_RUNNER_SUBSCRIPTION_RULE

        await stream.exchange([])
        await activities.create_or_update_branch_pr(_branch_pr_input("organisation"))

    [request] = runtime.requests
    assert request.auth_mode == AuthMode.API_KEY
    attempt_bearer = dict(request.extra_env)["OPENAI_API_KEY"]
    assert attempt_bearer != "sk-funder-key"
    assert runtime.status == 200
    assert seen == ["Bearer sk-funder-key"]
    [record] = proxy.outbox.pending()
    assert (record.contract_id, record.prompt_tokens) == (UUID(CONTRACT_ID), 12)


_CODEX_LIMIT_STDOUT = (
    '{"type": "error", "message": "You\'ve hit your usage limit for GPT-5. Switch to another '
    'model now, or try again at 11:31 PM (America/Chicago)."}'
)


@dataclass
class _LimitedCodex(_Codex):
    """A Codex whose person's plan is spent: it prints the limit and exits 1."""

    async def execute_directive(self, request: DirectiveRequest) -> DirectiveResult:
        result = await super().execute_directive(request)
        return replace(result, exit_code=1, stdout=_CODEX_LIMIT_STDOUT)


@pytest.mark.asyncio
async def test_a_subscription_usage_limit_returns_a_hold_and_pushes_nothing(
    tmp_path: Path,
) -> None:
    client = _Client()
    activities = _activities(client, _LimitedCodex(), tmp_path=tmp_path, host_party="user")

    output = await activities.create_or_update_branch_pr(_branch_pr_input("user"))

    assert output.harness_hold == HarnessHold(
        kind=HarnessHoldKind.USAGE_LIMIT, retry_not_before=_next_chicago_2331()
    )
    assert (output.pr_created, output.branch_head_sha) == (False, "")
    git = activities._git_workspace
    assert isinstance(git, FakeGitWorkspace)
    assert not {"commit_all", "push_branch"} & {call.operation for call in git.calls}
    [event] = client.payloads(HARNESS_HOLD_SOURCE)
    assert event == {
        "event": "directive.harness_hold",
        "kind": "usage_limit",
        "cli_kind": "codex_cli",
        "retry_not_before": output.harness_hold.retry_not_before,
        "directive_number": 1,
        "agent_id": None,
        "contract_id": CONTRACT_ID,
    }
    assert "usage limit" not in repr(event)


@pytest.mark.asyncio
async def test_an_api_key_directive_is_never_classified(tmp_path: Path) -> None:
    # The same output on the proxy is the proxy's limit, so it fails as it did before.
    client = _Client()

    with pytest.raises(RuntimeError, match="Codex CLI edit failed"):
        await _activities(
            client, _LimitedCodex(), tmp_path=tmp_path, host_party="user", proxy=_Proxy(holds=True)
        ).create_or_update_branch_pr(_branch_pr_input("user"))

    assert client.payloads(HARNESS_HOLD_SOURCE) == []


def _next_chicago_2331() -> str:
    chicago = datetime.now(ZoneInfo("America/Chicago"))
    reset = chicago.replace(hour=23, minute=31, second=0, microsecond=0)
    if reset <= chicago:
        reset += timedelta(days=1)
    return reset.astimezone(UTC).isoformat()
