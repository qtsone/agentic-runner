"""Each Directive's auth mode, through the real activity (local-agents 04).

What only the activity can show: the mode reaches the Agent Runtime on the request, a
shared Runner's refusal is non-retryable with Evidence naming the rule and spawns nothing,
a shared Runner that runs the Contract as its own uid with a login in its harness root
runs the subscription (QTS-1450), and a login left in a Contract's harness root on a
shared Runner that cannot use it is reported once, as a count.
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
from agentic_runner.auth_mode import (
    HOST_KEY_NOT_HELD_RULE,
    LLM_KEY_RELEASE_REFUSED_RULE,
    LLM_KEY_UNOPENABLE_RULE,
    POOL_KEY_NOT_HELD_RULE,
    SUBSCRIPTION_NEEDS_CONTRACT_UID_RULE,
    SUBSCRIPTION_NEEDS_HARNESS_LOGIN_RULE,
    SUBSCRIPTION_NO_RIGHT_RULE,
    SUBSCRIPTION_NOT_ACKNOWLEDGED_RULE,
    SUBSCRIPTION_NOT_SIGNED_IN_RULE,
    SUBSCRIPTION_OFF_PERSONAL_RUNNER_RULE,
)
from agentic_runner.credentials import CredentialResolver, EmptyCredentialStore
from agentic_runner.harness_self_test import DirectivesInFlight
from agentic_runner.integrations.git.fake_workspace import FakeGitWorkspace
from agentic_runner.integrations.github.fake_client import FakeGitHubClient
from agentic_runner.llm_proxy import CredentialSlot, LlmProxy, SlotStore
from agentic_runner.pool_key import LlmKeyOpener, parse_pool_key
from agentic_runner.registration import RunnerState
from agentic_runner.sealed_box import (
    RecipientKeyStore,
    SealedCredentialStream,
    generate_recipient_key,
    seal,
)
from agentic_runner.usage_windows import UsageWindows
from agentic_runner.workers.agent_runtime import (
    AuthMode,
    DirectiveEvidence,
    DirectiveRequest,
    DirectiveResult,
    RuntimeCapabilities,
)
from agentic_runner.workers.contract_isolation import ContractIsolation, DirectiveSandbox
from agentic_runner.workers.fastapi_client import WorkerFastApiClientRejectionError
from agentic_runner_contracts import __version__ as contracts_version
from agentic_runner_contracts.activity_io import (
    BranchPullRequestInput,
    HarnessHold,
    HarnessHoldKind,
)
from agentic_runner_contracts.llm_access import LlmAccess
from agentic_runner_contracts.llm_key import (
    platform_share_binding,
    pool_share_binding,
    split_llm_key,
)
from agentic_runner_contracts.routing import DirectiveRouting, RunnerRoutingIdentity
from agentic_runner_contracts.runner_registration import (
    ContractLlmTerms,
    FloorState,
    HeartbeatAck,
)
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
    """Holds a slot for the Contract or not, and the host store's key or not; hands each
    attempt the proxy pair and remembers what it was admitted with."""

    def __init__(self, *, holds: bool, host_key: bool = False) -> None:
        self.slots = SimpleNamespace(
            holds=lambda _contract_id, _provider, delivered_only=False: (
                holds or (host_key and not delivered_only)
            ),
            holds_host=lambda _provider: host_key,
        )
        self.attempts = 0
        self.admitted: list[dict[str, Any]] = []

    @contextlib.asynccontextmanager
    async def attempt(self, **admitted: Any) -> AsyncIterator[Any]:
        self.attempts += 1
        self.admitted.append(admitted)
        yield SimpleNamespace(
            env=lambda _cli_kind: {
                "OPENAI_BASE_URL": "http://127.0.0.1:1/a/x/v1",
                "OPENAI_API_KEY": "attempt-bearer",
            }
        )


def _isolation(tmp_path: Path, *, can_separate_uids: bool = False) -> ContractIsolation:
    return ContractIsolation(
        workspace_root=tmp_path,
        state_dir=tmp_path / ".state",
        uid_min=20_000,
        uid_max=20_100,
        max_processes=64,
        memory_limit_bytes=1 << 30,
        can_separate_uids=can_separate_uids,
    )


@pytest.fixture
def contract_uids(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ContractIsolation:
    """A Runner that runs each Contract as its own uid. The chown that hands a tree to that
    uid needs root, which the uid line's own test has (`test_contract_uid_isolation.py`);
    here only the decision is under test, so it is a no-op."""

    monkeypatch.setattr("os.chown", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("os.lchown", lambda *_args, **_kwargs: None)
    return _isolation(tmp_path, can_separate_uids=True)


def _sign_in(isolation: ContractIsolation, login_file: str = "auth.json") -> Path:
    login = isolation.harness_config_dir(CONTRACT_ID, "codex_cli") / login_file
    login.parent.mkdir(parents=True, exist_ok=True)
    login.write_text('{"tokens": {"refresh_token": "never-read"}}')
    return login


def _activities(
    client: _Client,
    runtime: _Codex,
    *,
    tmp_path: Path,
    operated: bool,
    tags: dict[str, str] | None = None,
    proxy: _Proxy | None = None,
    isolation: ContractIsolation | None = None,
    usage_windows: UsageWindows | None = None,
    cli_kind: str = "codex_cli",
    llm_contracts: list[ContractLlmTerms] | None = None,
    llm_keys: LlmKeyOpener | None = None,
) -> RunnerRalphActivities:
    return RunnerRalphActivities(
        client,  # type: ignore[arg-type]
        agent_runtimes={cli_kind: runtime},
        git_workspace=FakeGitWorkspace(status_evidence=" M README.md", diff_evidence="+change"),
        github_client=FakeGitHubClient(),
        workspace_root=tmp_path,
        routing_identity=RunnerRoutingIdentity(
            runner_id=RUNNER_ID, tags=dict(tags or {}), operated=operated
        ),
        llm_proxy=proxy,  # type: ignore[arg-type]
        contract_isolation=isolation,
        usage_windows=usage_windows,
        llm_contracts=lambda: llm_contracts,
        llm_keys=llm_keys,
    )


def _branch_pr_input(access: LlmAccess | None = None) -> BranchPullRequestInput:
    return BranchPullRequestInput(
        work_record_id=WORK_RECORD_ID,
        repository="qts/agentic-os",
        base_ref="main",
        branch_name=work_branch_name(work_record_id=WORK_RECORD_ID, profile_slug="engineer"),
        pr_title="Add a Production section",
        pr_body="body",
        routing=DirectiveRouting(runner_id=RUNNER_ID, llm_access=access),
    )


# ------------------------------------------------------------------ the named access (rev 3)


@pytest.mark.asyncio
async def test_without_named_access_a_pool_runner_never_spends_its_stores_key(
    tmp_path: Path,
) -> None:
    """A control plane below 3.25 names no access and caps none of a pool's store spend,
    so a pool Runner keeps today's choice: its store's key is not a key present."""

    client = _Client()
    runtime = _Codex()
    proxy = _Proxy(holds=False, host_key=True)

    with pytest.raises(ApplicationError) as refused:
        await _activities(
            client, runtime, tmp_path=tmp_path, operated=False, proxy=proxy
        ).create_or_update_branch_pr(_branch_pr_input())

    assert refused.value.type == SUBSCRIPTION_NEEDS_CONTRACT_UID_RULE
    assert (runtime.requests, proxy.attempts) == ([], 0)


@pytest.mark.asyncio
async def test_without_named_access_a_personal_runner_spends_its_stores_key(
    tmp_path: Path,
) -> None:
    runtime = _Codex()
    proxy = _Proxy(holds=False, host_key=True)

    await _activities(
        _Client(), runtime, tmp_path=tmp_path, operated=True, proxy=proxy
    ).create_or_update_branch_pr(_branch_pr_input())

    [request] = runtime.requests
    assert request.auth_mode == AuthMode.API_KEY
    [admitted] = proxy.admitted
    assert (admitted["host_key_only"], admitted["delivered_only"]) == (False, False)


@pytest.mark.asyncio
async def test_a_host_key_directive_spends_only_the_stores_key(tmp_path: Path) -> None:
    runtime = _Codex()
    proxy = _Proxy(holds=False, host_key=True)

    await _activities(
        _Client(), runtime, tmp_path=tmp_path, operated=False, proxy=proxy
    ).create_or_update_branch_pr(_branch_pr_input(LlmAccess(kind="host_key")))

    [request] = runtime.requests
    assert request.auth_mode == AuthMode.API_KEY
    [admitted] = proxy.admitted
    assert (admitted["provider"], admitted["host_key_only"]) == ("openai", True)


@pytest.mark.asyncio
async def test_a_signed_in_subscription_runs_although_a_key_is_held(tmp_path: Path) -> None:
    """Routed to the subscription, the Runner runs it: a key it also holds is not a
    reason to switch, any more than a sign-in is a reason to leave a key."""

    isolation = _isolation(tmp_path)
    _sign_in(isolation)
    runtime = _Codex()
    proxy = _Proxy(holds=True, host_key=True)

    await _activities(
        _Client(), runtime, tmp_path=tmp_path, operated=True, proxy=proxy, isolation=isolation
    ).create_or_update_branch_pr(_branch_pr_input(LlmAccess(kind="subscription")))

    [request] = runtime.requests
    assert request.auth_mode == AuthMode.SUBSCRIPTION
    assert proxy.attempts == 0


@pytest.mark.parametrize(
    ("access", "operated", "signed_in", "rule"),
    [
        # A delivered key and a sign-in are both still here; neither stands in.
        (LlmAccess(kind="host_key"), True, True, HOST_KEY_NOT_HELD_RULE),
        (LlmAccess(kind="subscription"), True, False, SUBSCRIPTION_NOT_SIGNED_IN_RULE),
        (
            LlmAccess(kind="subscription"),
            False,
            True,
            SUBSCRIPTION_OFF_PERSONAL_RUNNER_RULE,
        ),
        (LlmAccess(kind="llm_key", llm_key_id="key-7"), False, True, "llm_key"),
    ],
)
@pytest.mark.asyncio
async def test_access_the_runner_no_longer_holds_fails_the_directive_closed(
    tmp_path: Path, access: LlmAccess, operated: bool, signed_in: bool, rule: str
) -> None:
    isolation = _isolation(tmp_path)
    if signed_in:
        _sign_in(isolation)
    client = _Client()
    runtime = _Codex()
    proxy = _Proxy(holds=True, host_key=False)

    with pytest.raises(ApplicationError) as refused:
        await _activities(
            client,
            runtime,
            tmp_path=tmp_path,
            operated=operated,
            proxy=proxy,
            isolation=isolation,
        ).create_or_update_branch_pr(_branch_pr_input(access))

    expected = POOL_KEY_NOT_HELD_RULE if rule == "llm_key" else rule
    assert refused.value.non_retryable is True
    assert refused.value.type == expected
    assert (runtime.requests, proxy.attempts) == ([], 0), "nothing runs on other access"
    [evidence] = client.payloads(AGENT_RUNTIME_EVIDENCE_SOURCE)
    assert evidence["event"] == "directive.llm_access_refused"
    assert (evidence["rule"], evidence["llm_access"]) == (expected, access.kind)


@pytest.mark.parametrize(
    ("terms", "rule"),
    [
        (
            [ContractLlmTerms(contract_id=UUID(CONTRACT_ID), subscription="no_right")],
            SUBSCRIPTION_NO_RIGHT_RULE,
        ),
        (
            [ContractLlmTerms(contract_id=UUID(CONTRACT_ID), subscription="not_acknowledged")],
            SUBSCRIPTION_NOT_ACKNOWLEDGED_RULE,
        ),
        # An ack that names other Contracts but not this one allows it nothing.
        ([ContractLlmTerms(contract_id=uuid4(), subscription="allowed")], "no_right"),
    ],
)
@pytest.mark.asyncio
async def test_a_subscription_the_last_ack_does_not_allow_fails_closed(
    tmp_path: Path, terms: list[ContractLlmTerms], rule: str
) -> None:
    """QTS-1751: the person's own, signed-in Runner still refuses a subscription the
    control plane's own ack says the Contract may not use, whatever routing named."""

    isolation = _isolation(tmp_path)
    _sign_in(isolation)
    client = _Client()
    runtime = _Codex()

    with pytest.raises(ApplicationError) as refused:
        await _activities(
            client,
            runtime,
            tmp_path=tmp_path,
            operated=True,
            isolation=isolation,
            llm_contracts=terms,
        ).create_or_update_branch_pr(_branch_pr_input(LlmAccess(kind="subscription")))

    expected = SUBSCRIPTION_NO_RIGHT_RULE if rule == "no_right" else rule
    assert refused.value.non_retryable is True
    assert refused.value.type == expected
    assert runtime.requests == []
    [evidence] = client.payloads(AGENT_RUNTIME_EVIDENCE_SOURCE)
    assert evidence["event"] == "directive.llm_access_refused"
    assert (evidence["rule"], evidence["llm_access"]) == (expected, "subscription")


@pytest.mark.asyncio
async def test_a_subscription_the_last_ack_allows_runs(tmp_path: Path) -> None:
    isolation = _isolation(tmp_path)
    _sign_in(isolation)
    runtime = _Codex()

    await _activities(
        _Client(),
        runtime,
        tmp_path=tmp_path,
        operated=True,
        isolation=isolation,
        llm_contracts=[ContractLlmTerms(contract_id=UUID(CONTRACT_ID), subscription="allowed")],
    ).create_or_update_branch_pr(_branch_pr_input(LlmAccess(kind="subscription")))

    [request] = runtime.requests
    assert request.auth_mode == AuthMode.SUBSCRIPTION


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
        operated=True,
        tags={"install_channel": install_channel},
    ).create_or_update_branch_pr(_branch_pr_input())

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
        _Client(), runtime, tmp_path=tmp_path, operated=True, proxy=proxy
    ).create_or_update_branch_pr(_branch_pr_input())

    [request] = runtime.requests
    assert request.auth_mode == AuthMode.API_KEY
    assert dict(request.extra_env)["OPENAI_API_KEY"] == "attempt-bearer"
    assert proxy.attempts == 1


@pytest.mark.asyncio
async def test_a_shared_runner_runs_codex_on_the_proxy_when_a_key_is_present(
    tmp_path: Path,
) -> None:
    runtime = _Codex()

    await _activities(
        _Client(), runtime, tmp_path=tmp_path, operated=False, proxy=_Proxy(holds=True)
    ).create_or_update_branch_pr(_branch_pr_input())

    [request] = runtime.requests
    assert request.auth_mode == AuthMode.API_KEY


@pytest.mark.asyncio
async def test_a_shared_runner_refuses_a_directive_that_would_need_a_subscription(
    tmp_path: Path,
) -> None:
    client = _Client()
    runtime = _Codex()

    with pytest.raises(ApplicationError) as refused:
        await _activities(
            client, runtime, tmp_path=tmp_path, operated=False, proxy=_Proxy(holds=False)
        ).create_or_update_branch_pr(_branch_pr_input())

    assert refused.value.non_retryable is True
    assert refused.value.type == SUBSCRIPTION_NEEDS_CONTRACT_UID_RULE
    assert runtime.requests == [], "nothing is spawned on a refused mode"
    [evidence] = client.payloads(AGENT_RUNTIME_EVIDENCE_SOURCE)
    assert evidence["event"] == "directive.auth_mode_refused"
    assert evidence["rule"] == SUBSCRIPTION_NEEDS_CONTRACT_UID_RULE
    assert evidence["operated"] is False


@pytest.mark.asyncio
async def test_a_shared_runner_without_contract_uids_refuses_even_with_a_login(
    tmp_path: Path,
) -> None:
    isolation = _isolation(tmp_path)
    _sign_in(isolation)
    runtime = _Codex()

    with pytest.raises(ApplicationError) as refused:
        await _activities(
            _Client(),
            runtime,
            tmp_path=tmp_path,
            operated=False,
            proxy=_Proxy(holds=False),
            isolation=isolation,
        ).create_or_update_branch_pr(_branch_pr_input())

    assert refused.value.type == SUBSCRIPTION_NEEDS_CONTRACT_UID_RULE
    assert runtime.requests == []


@pytest.mark.asyncio
async def test_a_customer_operated_cluster_runner_runs_a_signed_in_subscription(
    tmp_path: Path, contract_uids: ContractIsolation
) -> None:
    """QTS-1450's acceptance: Contract uids and a login in that Contract's harness root."""

    _sign_in(contract_uids)
    runtime = _Codex()

    await _activities(
        _Client(),
        runtime,
        tmp_path=tmp_path,
        operated=False,
        proxy=_Proxy(holds=False),
        isolation=contract_uids,
    ).create_or_update_branch_pr(_branch_pr_input())

    [request] = runtime.requests
    assert request.auth_mode == AuthMode.SUBSCRIPTION
    assert "OPENAI_API_KEY" not in dict(request.extra_env)
    assert request.sandbox is not None and request.sandbox.uid == contract_uids.uid_for(CONTRACT_ID)


@pytest.mark.asyncio
async def test_a_customer_operated_cluster_runner_refuses_a_contract_not_signed_in(
    tmp_path: Path, contract_uids: ContractIsolation
) -> None:
    client = _Client()
    runtime = _Codex()
    # Another Contract's login is no login for this one.
    other = contract_uids.harness_config_dir(str(uuid4()), "codex_cli") / "auth.json"
    other.parent.mkdir(parents=True)
    other.write_text("{}")

    with pytest.raises(ApplicationError) as refused:
        await _activities(
            client,
            runtime,
            tmp_path=tmp_path,
            operated=False,
            proxy=_Proxy(holds=False),
            isolation=contract_uids,
        ).create_or_update_branch_pr(_branch_pr_input())

    assert refused.value.non_retryable is True
    assert refused.value.type == SUBSCRIPTION_NEEDS_HARNESS_LOGIN_RULE
    assert runtime.requests == []
    [evidence] = client.payloads(AGENT_RUNTIME_EVIDENCE_SOURCE)
    assert evidence["rule"] == SUBSCRIPTION_NEEDS_HARNESS_LOGIN_RULE


@pytest.mark.asyncio
async def test_a_login_left_on_a_shared_runner_is_reported_once_as_a_count(
    tmp_path: Path,
) -> None:
    isolation = _isolation(tmp_path)
    login = _sign_in(isolation)
    client = _Client()
    activities = _activities(
        client,
        _Codex(),
        tmp_path=tmp_path,
        operated=False,
        proxy=_Proxy(holds=True),
        isolation=isolation,
    )

    await activities.create_or_update_branch_pr(_branch_pr_input())
    await activities.create_or_update_branch_pr(_branch_pr_input())

    [report] = client.payloads(HARNESS_LOGIN_RESIDUE_SOURCE)
    assert report == {
        "event": "harness.login_residue",
        "contract_id": CONTRACT_ID,
        "operated": False,
        "login_files": {"codex_cli": 1},
    }
    assert str(tmp_path) not in repr(client.evidence)
    assert "never-read" not in repr(client.evidence)
    assert login.is_file(), "the Runner reports the login; the Contract's wipe removes it"


@pytest.mark.asyncio
async def test_a_login_a_shared_runner_can_run_on_is_not_residue(
    tmp_path: Path, contract_uids: ContractIsolation
) -> None:
    _sign_in(contract_uids)
    client = _Client()

    await _activities(
        client,
        _Codex(),
        tmp_path=tmp_path,
        operated=False,
        proxy=_Proxy(holds=True),
        isolation=contract_uids,
    ).create_or_update_branch_pr(_branch_pr_input())

    assert client.payloads(HARNESS_LOGIN_RESIDUE_SOURCE) == []


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
            operated=False,
            proxy=proxy,  # type: ignore[arg-type]
        )
        with pytest.raises(ApplicationError) as refused:
            await activities.create_or_update_branch_pr(_branch_pr_input())
        assert refused.value.type == SUBSCRIPTION_NEEDS_CONTRACT_UID_RULE

        await stream.exchange([])
        await activities.create_or_update_branch_pr(_branch_pr_input())

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
    activities = _activities(client, _LimitedCodex(), tmp_path=tmp_path, operated=True)

    output = await activities.create_or_update_branch_pr(_branch_pr_input())

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
async def test_a_subscription_usage_limit_makes_the_usage_window_probe_due(
    tmp_path: Path,
) -> None:
    probes: list[DirectiveSandbox] = []

    async def probe(sandbox: DirectiveSandbox) -> list[Any]:
        probes.append(sandbox)
        return []

    usage = UsageWindows(
        probes={"codex_cli": probe},
        sandbox_for=lambda _contract, _kind: DirectiveSandbox(
            home_dir=tmp_path,
            harness_config_dir=tmp_path,
            max_processes=None,
            max_memory_bytes=None,
        ),
        in_flight=DirectivesInFlight(),
        # Time stands still: only the usage limit can make the next probe due.
        monotonic=lambda: 0.0,
    )
    targets = [(CONTRACT_ID, "codex_cli")]
    await usage.run_due(targets)

    await _activities(
        _Client(), _LimitedCodex(), tmp_path=tmp_path, operated=True, usage_windows=usage
    ).create_or_update_branch_pr(_branch_pr_input())
    await usage.run_due(targets)

    assert len(probes) == 2


@pytest.mark.asyncio
async def test_an_api_key_directive_is_never_classified(tmp_path: Path) -> None:
    # The same output on the proxy is the proxy's limit, so it fails as it did before.
    client = _Client()

    with pytest.raises(RuntimeError, match="Codex CLI edit failed"):
        await _activities(
            client, _LimitedCodex(), tmp_path=tmp_path, operated=True, proxy=_Proxy(holds=True)
        ).create_or_update_branch_pr(_branch_pr_input())

    assert client.payloads(HARNESS_HOLD_SOURCE) == []


def _next_chicago_2331() -> str:
    chicago = datetime.now(ZoneInfo("America/Chicago"))
    reset = chicago.replace(hour=23, minute=31, second=0, microsecond=0)
    if reset <= chicago:
        reset += timedelta(days=1)
    return reset.astimezone(UTC).isoformat()


@pytest.mark.asyncio
async def test_the_agents_model_runs_and_its_evidence_and_usage_name_it(tmp_path: Path) -> None:
    """ADR-0021: the runtime context's model reaches the Agent Runtime, and what the
    runtime says ran is what the Evidence and the harness Usage Record carry."""

    class _ModelClient(_Client):
        def __init__(self) -> None:
            super().__init__()
            self.usage: list[dict[str, Any]] = []

        async def get_runtime_context(self, work_record_id: str) -> dict[str, Any]:
            return {**await super().get_runtime_context(work_record_id), "model": "gpt-5-codex"}

        async def report_harness_usage(self, payload: Mapping[str, Any]) -> list[dict[str, Any]]:
            self.usage.append(dict(payload))
            return []

    @dataclass
    class _RunsTheModel(_Codex):
        async def execute_directive(self, request: DirectiveRequest) -> DirectiveResult:
            result = await super().execute_directive(request)
            usage = '{"type":"turn.completed","usage":{"input_tokens":3,"output_tokens":1}}'
            return replace(result, stdout=usage, model=request.model)

    client = _ModelClient()
    runtime = _RunsTheModel()

    await _activities(client, runtime, tmp_path=tmp_path, operated=True).create_or_update_branch_pr(
        _branch_pr_input()
    )

    [request] = runtime.requests
    assert request.model == "gpt-5-codex"
    [evidence] = client.payloads("ralph.codex_run")
    assert evidence["cli_kind"] == "codex_cli"
    assert evidence["model"] == "gpt-5-codex"
    [usage] = client.usage
    assert usage["model_name"] == "gpt-5-codex"


# ------------------------------------------------------------------ the pool's LLM Keys (rev 3)

POOL_ID = "3b8e2f61-7c4a-4d19-9e05-6a1f0c2d8b47"
LLM_KEY_ID = "c41d9a72-5e3b-4f80-8a16-2d7e9b0f3c58"
POOL_LLM_KEY = "sk-proj-the-pools-openai-key"


class _ReleasingClient(_Client):
    """A control plane holding one LLM Key of the pool, as the admin's browser stored it."""

    def __init__(self, release: dict[str, Any] | Exception) -> None:
        super().__init__()
        self.release = release
        self.requests: list[tuple[str, str, dict[str, Any]]] = []

    async def release_llm_key(
        self, work_record_id: str, llm_key_id: str, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        self.requests.append((work_record_id, llm_key_id, dict(payload)))
        if isinstance(self.release, Exception):
            raise self.release
        return dict(self.release)


def _pool(tmp_path: Path, *, provider: str = "openai") -> tuple[LlmKeyOpener, dict[str, Any]]:
    """A pool Runner's opener, and QTS's release of the pool's key for it."""

    pool_key = parse_pool_key(generate_recipient_key().private_key)
    recipient_keys = RecipientKeyStore(tmp_path / "state")
    recipient = recipient_keys.current()
    pool_share, platform_share = split_llm_key(POOL_LLM_KEY)
    release = {
        "llm_key_id": LLM_KEY_ID,
        "pool_id": POOL_ID,
        "provider": provider,
        "pool_key_fingerprint": pool_key.fingerprint,
        "pool_share": seal(
            public_key=pool_key.public_key,
            binding=pool_share_binding(
                pool_id=POOL_ID, llm_key_id=LLM_KEY_ID, pool_key_fingerprint=pool_key.fingerprint
            ),
            plaintext=pool_share,
        ),
        "recipient_key_id": recipient.key_id,
        "platform_share": seal(
            public_key=recipient.public_key,
            binding=platform_share_binding(
                llm_key_id=LLM_KEY_ID, recipient_key_id=recipient.key_id
            ),
            plaintext=platform_share,
        ),
    }
    return LlmKeyOpener(pool_key=pool_key, recipient_keys=recipient_keys), release


def _llm_key_input() -> BranchPullRequestInput:
    return _branch_pr_input(LlmAccess(kind="llm_key", llm_key_id=LLM_KEY_ID))


@pytest.mark.asyncio
async def test_a_released_llm_key_is_held_in_memory_for_one_directive_and_gone_after_it(
    tmp_path: Path,
) -> None:
    seen: list[str] = []

    def provider(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["authorization"])
        return httpx.Response(200, json={"usage": {"input_tokens": 12, "output_tokens": 3}})

    proxy = LlmProxy(
        slots=SlotStore(), client=httpx.AsyncClient(transport=httpx.MockTransport(provider))
    )
    opener, release = _pool(tmp_path)
    client = _ReleasingClient(release)
    runtime = _SpendingCodex()

    async with proxy:
        await _activities(
            client,
            runtime,
            tmp_path=tmp_path,
            operated=False,
            proxy=proxy,  # type: ignore[arg-type]
            llm_keys=opener,
        ).create_or_update_branch_pr(_llm_key_input())

        [request] = runtime.requests
        env = dict(request.extra_env)
        async with httpx.AsyncClient() as late:
            after = await late.post(
                f"{env['OPENAI_BASE_URL']}/responses",
                json={"model": "gpt-5", "input": "again"},
                headers={"Authorization": f"Bearer {env['OPENAI_API_KEY']}"},
            )

    assert request.auth_mode == AuthMode.API_KEY
    assert (runtime.status, seen) == (200, [f"Bearer {POOL_LLM_KEY}"])
    # The Agent Runtime holds the attempt's bearer only, never the key.
    assert POOL_LLM_KEY not in env.values()
    # Gone with the Directive: the attempt is closed, and the key never reached the slot
    # store that serves every other Directive and the heartbeat.
    assert after.status_code == 401
    assert proxy.slots.statuses() == []
    assert proxy.slots.resolve(UUID(CONTRACT_ID), "openai") is None
    [(work_record_id, llm_key_id, asked)] = client.requests
    assert (work_record_id, llm_key_id) == (WORK_RECORD_ID, LLM_KEY_ID)
    assert asked["pool_key_fingerprint"] == opener.fingerprint
    assert asked["directive_id"].startswith(f"{WORK_RECORD_ID}:")
    assert POOL_LLM_KEY not in repr(client.evidence)


@pytest.mark.parametrize(
    ("change", "rule"),
    [
        ("old_pool_key", LLM_KEY_UNOPENABLE_RULE),
        ("other_provider", LLM_KEY_UNOPENABLE_RULE),
        ("other_key", LLM_KEY_UNOPENABLE_RULE),
        ("refused", LLM_KEY_RELEASE_REFUSED_RULE),
    ],
)
@pytest.mark.asyncio
async def test_an_llm_key_that_is_refused_or_does_not_open_fails_the_directive_closed(
    tmp_path: Path, change: str, rule: str
) -> None:
    """Nothing runs, on this key or on any other access, and the Evidence names the rule."""

    opener, release = _pool(
        tmp_path, provider="anthropic" if change == "other_provider" else "openai"
    )
    answer: dict[str, Any] | Exception = release
    if change == "old_pool_key":
        # Installed before the admin replaced the Pool Key: the share is sealed to the new one.
        opener = LlmKeyOpener(
            pool_key=parse_pool_key(generate_recipient_key().private_key),
            recipient_keys=RecipientKeyStore(tmp_path / "state"),
        )
    elif change == "other_key":
        answer = {**release, "llm_key_id": "0e5f8a31-9b2c-4d7e-a610-3c8b1f4d2e95"}
    elif change == "refused":
        answer = WorkerFastApiClientRejectionError("refused", status_code=403)
    client = _ReleasingClient(answer)
    runtime = _Codex()
    proxy = _Proxy(holds=True, host_key=True)

    with pytest.raises(ApplicationError) as refused:
        await _activities(
            client,
            runtime,
            tmp_path=tmp_path,
            operated=False,
            proxy=proxy,
            llm_keys=opener,
        ).create_or_update_branch_pr(_llm_key_input())

    assert refused.value.non_retryable is True
    assert refused.value.type == rule
    assert (runtime.requests, proxy.attempts) == ([], 0), "nothing runs on other access"
    [evidence] = client.payloads(AGENT_RUNTIME_EVIDENCE_SOURCE)
    assert (evidence["event"], evidence["rule"]) == ("directive.llm_access_refused", rule)
