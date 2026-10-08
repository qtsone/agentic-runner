"""A shared Runner runs API keys only; Codex API-key mode (local-agents 04).

The Runner chooses each Directive's auth mode itself, from its registered host party and
whether a key is present -- never from the payload, never from how it was installed --
and refuses what a shared Runner must not do. These are the unit-level halves; the
activity-level ones (Evidence, residue, the per-Directive wiring) are in
``tests/integration/test_runner_auth_mode_directives.py``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest
from temporalio.exceptions import ApplicationError

from agentic_runner.auth_mode import (
    SETUP_TOKEN_REFUSED_RULE,
    SHARED_RUNNER_SIGN_IN_RULE,
    SHARED_RUNNER_SUBSCRIPTION_RULE,
    AuthModeRefusedError,
    choose_auth_mode,
)
from agentic_runner.device_login_activities import ContractDeviceLoginActivities
from agentic_runner.llm_proxy import CredentialSlot, LlmProxy, SlotStore
from agentic_runner.sealed_box import (
    RecipientKeyStore,
    SealedCredentialStream,
    seal,
)
from agentic_runner.workers._runtime_support import RESERVED_DIRECTIVE_ENV, apply_extra_env
from agentic_runner.workers.agent_runtime import AuthMode, DirectiveRequest
from agentic_runner.workers.claude_runtime import ClaudeRuntime
from agentic_runner.workers.claude_sign_in import ClaudeAuthStatus
from agentic_runner.workers.codex_runtime import CodexRuntime, SubprocessResult
from agentic_runner.workers.command_policy import CommandPolicy, evaluate_command_policy
from agentic_runner.workers.contract_device_login import DeviceLoginPrompt
from agentic_runner.workers.contract_isolation import ContractIsolation
from agentic_runner.workers.settings import WorkerSettings
from agentic_runner_contracts.activity_io import (
    ContractDeviceLoginInput,
    ContractDeviceLoginStatusInput,
)
from agentic_runner_contracts.sealed_credential import SealedCredential, delivery_binding

CONTRACT = UUID("11111111-1111-4111-8111-111111111111")
CODEX_MODES = CodexRuntime.auth_modes
CLAUDE_MODES = ClaudeRuntime.auth_modes


# ------------------------------------------------------------------ the mode table

# host party x key present x cli_kind -> the mode, or the rule that refuses it.
MODE_TABLE: list[tuple[str | None, bool, str, frozenset[AuthMode], AuthMode | str]] = [
    ("organisation", True, "codex_cli", CODEX_MODES, AuthMode.API_KEY),
    ("organisation", True, "claude_code", CLAUDE_MODES, AuthMode.API_KEY),
    ("organisation", False, "codex_cli", CODEX_MODES, SHARED_RUNNER_SUBSCRIPTION_RULE),
    ("organisation", False, "claude_code", CLAUDE_MODES, SHARED_RUNNER_SUBSCRIPTION_RULE),
    ("account", True, "codex_cli", CODEX_MODES, AuthMode.API_KEY),
    ("account", True, "claude_code", CLAUDE_MODES, AuthMode.API_KEY),
    ("account", False, "codex_cli", CODEX_MODES, SHARED_RUNNER_SUBSCRIPTION_RULE),
    ("account", False, "claude_code", CLAUDE_MODES, SHARED_RUNNER_SUBSCRIPTION_RULE),
    ("user", True, "codex_cli", CODEX_MODES, AuthMode.API_KEY),
    ("user", True, "claude_code", CLAUDE_MODES, AuthMode.API_KEY),
    ("user", False, "codex_cli", CODEX_MODES, AuthMode.SUBSCRIPTION),
    # Claude Code subscription mode is local-agents 07; until then it stays on a key.
    ("user", False, "claude_code", CLAUDE_MODES, AuthMode.API_KEY),
    # An unregistered process is not the person's own Runner: never a subscription.
    (None, False, "codex_cli", CODEX_MODES, AuthMode.API_KEY),
    (None, True, "codex_cli", CODEX_MODES, AuthMode.API_KEY),
]


@pytest.mark.parametrize(
    ("host_party", "key_present", "cli_kind", "modes", "expected"),
    MODE_TABLE,
    ids=[f"{row[0]}-{'key' if row[1] else 'nokey'}-{row[2]}" for row in MODE_TABLE],
)
def test_the_mode_is_chosen_from_host_party_and_key_presence(
    host_party: str | None,
    key_present: bool,
    cli_kind: str,
    modes: frozenset[AuthMode],
    expected: AuthMode | str,
) -> None:
    if isinstance(expected, AuthMode):
        assert (
            choose_auth_mode(host_party=host_party, key_present=key_present, runtime_modes=modes)
            == expected
        )
        return
    with pytest.raises(AuthModeRefusedError) as refused:
        choose_auth_mode(host_party=host_party, key_present=key_present, runtime_modes=modes)
    assert refused.value.rule == expected


def test_a_shared_runner_never_chooses_a_subscription() -> None:
    for host_party in ("organisation", "account"):
        for key_present in (True, False):
            try:
                mode = choose_auth_mode(
                    host_party=host_party, key_present=key_present, runtime_modes=CODEX_MODES
                )
            except AuthModeRefusedError:
                continue
            assert mode == AuthMode.API_KEY


# ------------------------------------------------------------------ Codex API-key mode


def _codex_settings(tmp_path: Path) -> WorkerSettings:
    return WorkerSettings(
        TEMPORAL_ADDRESS="127.0.0.1:7233",
        INTERNAL_FASTAPI_BASE_URL="http://agentic-api.internal:8000",
        WORKSPACE_ROOT=tmp_path / "workspaces",
        CODEX_HOME=tmp_path / "codex-home",
        CODEX_SANDBOX_MODE="workspace-write",
        CODEX_ASK_FOR_APPROVAL="never",
        CODEX_STRICT_CONFIG=True,
        CODEX_POLICY_HOOK_CONFIGURED=True,
    )


def _workspace(settings: WorkerSettings) -> Path:
    workspace = settings.WORKSPACE_ROOT / "wr" / "repo"
    workspace.mkdir(parents=True)
    return workspace


def _responses_provider(seen: list[Mapping[str, str]]) -> httpx.MockTransport:
    """An upstream speaking the Responses API's stream, usage on `response.completed`."""

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append({"path": request.url.path, "authorization": request.headers["authorization"]})
        completed = {
            "type": "response.completed",
            "response": {
                "id": "resp_1",
                "usage": {
                    "input_tokens": 1_200,
                    "output_tokens": 340,
                    "input_tokens_details": {"cached_tokens": 200},
                },
            },
        }
        frames = (
            b'event: response.created\ndata: {"type":"response.created"}\n\n'
            b"event: response.completed\ndata: " + json.dumps(completed).encode() + b"\n\n"
        )
        return httpx.Response(200, content=frames)

    return httpx.MockTransport(handle)


def _acts_like_codex(calls: list[dict[str, Any]]) -> Any:
    """A subprocess runner that does what codex does with the overrides it is given:
    read the provider's base URL off argv and its key from the env var `env_key` names,
    then POST a streamed Responses request there."""

    async def run(**kwargs: Any) -> SubprocessResult:
        argv: list[str] = kwargs["argv"]
        env: dict[str, str] = kwargs["env"]
        calls.append({"argv": argv, "env": env})
        provider = next(arg for arg in argv if arg.startswith("model_providers."))
        base_url = re.search(r'base_url="([^"]+)"', provider)
        env_key = re.search(r'env_key="([^"]+)"', provider)
        assert base_url is not None and env_key is not None
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{base_url.group(1)}/responses",
                json={"model": "gpt-5", "input": "go", "stream": True},
                headers={"Authorization": f"Bearer {env[env_key.group(1)]}"},
            )
        return SubprocessResult(
            exit_code=0 if response.status_code == 200 else 1, stdout="", stderr=""
        )

    return run


@pytest.mark.asyncio
async def test_a_codex_api_key_directive_reaches_the_proxy_on_the_attempt_bearer(
    tmp_path: Path,
) -> None:
    async def always_valid(_: CredentialSlot) -> bool:
        return True

    slots = SlotStore(probe=always_valid)
    await slots.put(
        CONTRACT,
        CredentialSlot(
            reference="contract_llm_key",
            key_id="key-a",
            provider_name="openai",
            base_url="https://provider.test/v1",
            value="sk-funder-key",
            runtime_kind="codex_cli",
        ),
    )
    seen: list[Mapping[str, str]] = []
    calls: list[dict[str, Any]] = []
    settings = _codex_settings(tmp_path)
    runtime = CodexRuntime(settings=settings, runner=_acts_like_codex(calls))

    async with (
        LlmProxy(
            slots=slots, client=httpx.AsyncClient(transport=_responses_provider(seen))
        ) as proxy,
        proxy.attempt(directive_id="wr-1:d1", contract_id=CONTRACT) as attempt,
    ):
        result = await runtime.execute_directive(
            DirectiveRequest(
                workspace_path=_workspace(settings),
                prompt="go",
                base_branch="main",
                work_branch="work",
                extra_env=tuple(attempt.env("codex_cli").items()),
                auth_mode=AuthMode.API_KEY,
            )
        )

    assert result.exit_code == 0
    # Upstream saw the funder's key at the Responses route, never the attempt bearer...
    assert seen == [{"path": "/v1/responses", "authorization": "Bearer sk-funder-key"}]
    # ...the subprocess held only the attempt bearer, and the call was metered here.
    [call] = calls
    assert call["env"]["OPENAI_API_KEY"] == attempt.token
    [record] = proxy.outbox.pending()
    assert (record.prompt_tokens, record.completion_tokens, record.cached_tokens) == (
        1_200,
        340,
        200,
    )
    # No path to a login file anywhere the harness is handed, and no reading it either.
    assert not any("auth.json" in arg for arg in call["argv"])
    assert not any("auth.json" in value for value in call["env"].values())
    assert 'cli_auth_credentials_store="ephemeral"' in call["argv"]
    assert 'model_provider="agentic_runner"' in call["argv"]


@pytest.mark.asyncio
async def test_a_codex_api_key_directive_without_the_proxy_pair_is_refused(
    tmp_path: Path,
) -> None:
    calls: list[dict[str, Any]] = []
    settings = _codex_settings(tmp_path)
    runtime = CodexRuntime(settings=settings, runner=_acts_like_codex(calls))

    result = await runtime.execute_directive(
        DirectiveRequest(
            workspace_path=_workspace(settings),
            prompt="go",
            base_branch="main",
            work_branch="work",
            auth_mode=AuthMode.API_KEY,
        )
    )

    assert calls == [], "with no endpoint the only credential left is the login it must not use"
    assert result.exit_code == 126
    assert result.evidence.guard_mode.startswith("refused: api_key mode")


@pytest.mark.asyncio
async def test_a_codex_subscription_directive_carries_no_provider_override(tmp_path: Path) -> None:
    calls: list[dict[str, Any]] = []
    settings = _codex_settings(tmp_path)

    async def record(**kwargs: Any) -> SubprocessResult:
        calls.append(kwargs)
        return SubprocessResult(exit_code=0, stdout="", stderr="")

    await CodexRuntime(settings=settings, runner=record).execute_directive(
        DirectiveRequest(
            workspace_path=_workspace(settings),
            prompt="go",
            base_branch="main",
            work_branch="work",
            auth_mode=AuthMode.SUBSCRIPTION,
        )
    )

    [call] = calls
    assert not any("model_provider" in arg for arg in call["argv"])
    assert not any("cli_auth_credentials_store" in arg for arg in call["argv"])


def test_the_command_floor_admits_the_ephemeral_store_and_nothing_shaped_like_it(
    tmp_path: Path,
) -> None:
    def decide(argument: str) -> bool:
        return evaluate_command_policy(
            argv=["codex", "exec", "--config", argument, "-"],
            cwd=tmp_path,
            workspace_root=tmp_path,
            base_branch="main",
            work_branch="work",
            policy=CommandPolicy(runtime_program="codex"),
        ).allowed

    assert decide('cli_auth_credentials_store="ephemeral"')
    assert not decide('cli_auth_credentials_store="file"')
    assert not decide("OPENAI_API_KEY=sk-123")


# ------------------------------------------------------------------ refusals


class _FakeClaudeSignIns:
    def __init__(self, login: _FakeDeviceLogin) -> None:
        self._login = login

    async def status(self, contract_id: str) -> ClaudeAuthStatus:
        self._login.calls += 1
        return ClaudeAuthStatus(logged_in=True, auth_method="oauth_token", subscription_type="max")


class _FakeDeviceLogin:
    def __init__(self) -> None:
        self.calls = 0
        self.methods: list[str | None] = []
        self.claude = _FakeClaudeSignIns(self)

    async def sign_in(
        self, contract_id: str, *, runtime_kind: str, method: str | None = None
    ) -> DeviceLoginPrompt:
        from datetime import UTC, datetime

        self.calls += 1
        self.methods.append(method)
        return DeviceLoginPrompt(
            contract_id=contract_id,
            runtime_kind=runtime_kind,
            verification_uri="https://auth.example.test/device",
            user_code="ABCD-1234",
            expires_at=datetime(2026, 10, 8, tzinfo=UTC),
        )

    def token_present(self, contract_id: str, *, runtime_kind: str) -> bool:
        self.calls += 1
        return False

    def token_delivered_at(self, contract_id: str, *, runtime_kind: str) -> float | None:
        return None


def _device_login_activities(
    tmp_path: Path, host_party: str | None
) -> tuple[ContractDeviceLoginActivities, _FakeDeviceLogin]:
    login = _FakeDeviceLogin()
    isolation = ContractIsolation(
        workspace_root=tmp_path / "workspaces",
        state_dir=tmp_path / "state",
        uid_min=20_000,
        uid_max=20_100,
        max_processes=64,
        memory_limit_bytes=1 << 30,
        can_separate_uids=False,
    )
    return (
        ContractDeviceLoginActivities(
            contract_isolation=isolation,
            device_login=login,  # type: ignore[arg-type]
            host_party=host_party,
        ),
        login,
    )


@pytest.mark.parametrize("host_party", ["organisation", "account", None])
@pytest.mark.asyncio
async def test_a_shared_runner_refuses_sign_in_and_its_status_check(
    tmp_path: Path, host_party: str | None
) -> None:
    activities, login = _device_login_activities(tmp_path, host_party)

    for call in (
        activities.sign_in_contract_device_login(
            ContractDeviceLoginInput(contract_id=str(CONTRACT), runtime_kind="codex_cli")
        ),
        activities.check_contract_device_login_status(
            ContractDeviceLoginStatusInput(contract_id=str(CONTRACT), runtime_kind="codex_cli")
        ),
    ):
        with pytest.raises(ApplicationError) as refused:
            await call
        assert refused.value.type == SHARED_RUNNER_SIGN_IN_RULE
        assert refused.value.non_retryable is True
    assert login.calls == 0, "the harness root is never touched on a shared Runner"


@pytest.mark.asyncio
async def test_the_persons_own_runner_still_signs_in(tmp_path: Path) -> None:
    activities, login = _device_login_activities(tmp_path, "user")

    result = await activities.sign_in_contract_device_login(
        ContractDeviceLoginInput(contract_id=str(CONTRACT), runtime_kind="codex_cli")
    )

    assert result.user_code == "ABCD-1234"
    assert login.calls == 1


@pytest.mark.parametrize("host_party", ["organisation", "account"])
@pytest.mark.parametrize("method", [None, "oauth_token"])
@pytest.mark.asyncio
async def test_a_shared_runner_makes_a_claude_long_lived_token(
    tmp_path: Path, host_party: str, method: str | None
) -> None:
    """Local-agents 21: a shared Runner may run a Claude subscription on the long-lived
    token, so it starts that sign-in -- the default method -- and reads its status."""

    activities, login = _device_login_activities(tmp_path, host_party)

    await activities.sign_in_contract_device_login(
        ContractDeviceLoginInput(
            contract_id=str(CONTRACT), runtime_kind="claude_code", method=method
        )
    )
    status = await activities.check_contract_device_login_status(
        ContractDeviceLoginStatusInput(contract_id=str(CONTRACT), runtime_kind="claude_code")
    )

    assert login.methods == [method]
    assert (status.token_present, status.auth_method, status.subscription_type) == (
        True,
        "oauth_token",
        "max",
    )


@pytest.mark.parametrize("host_party", ["organisation", "account", None])
@pytest.mark.asyncio
async def test_a_shared_runner_refuses_the_short_lived_claude_sign_in(
    tmp_path: Path, host_party: str | None
) -> None:
    activities, login = _device_login_activities(tmp_path, host_party)

    with pytest.raises(ApplicationError) as refused:
        await activities.sign_in_contract_device_login(
            ContractDeviceLoginInput(
                contract_id=str(CONTRACT), runtime_kind="claude_code", method="claude_ai"
            )
        )

    assert refused.value.type == SHARED_RUNNER_SIGN_IN_RULE
    assert login.calls == 0


@pytest.mark.asyncio
async def test_an_unregistered_runner_refuses_even_the_long_lived_token(tmp_path: Path) -> None:
    activities, login = _device_login_activities(tmp_path, None)

    with pytest.raises(ApplicationError):
        await activities.sign_in_contract_device_login(
            ContractDeviceLoginInput(contract_id=str(CONTRACT), runtime_kind="claude_code")
        )
    assert login.calls == 0


def _sealed(store: RecipientKeyStore, value: str, *, slot: str = "llm_key") -> SealedCredential:
    key = store.current()
    return SealedCredential(
        contract_id=CONTRACT,
        slot=slot,
        recipient_key_id=key.key_id,
        version=1,
        ciphertext=seal(
            public_key=key.public_key,
            binding=delivery_binding(contract_id=CONTRACT, slot=slot, recipient_key_id=key.key_id),
            plaintext=value,
        ),
    )


def test_a_setup_token_is_refused_on_every_runner_including_the_persons_own(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # The sealed stream has no host party at all: the refusal holds on a user-hosted
    # Runner exactly as on a shared one, because nothing here could tell them apart.
    store = RecipientKeyStore(tmp_path)
    stream = SealedCredentialStream(store)

    refused = stream.apply(
        [
            _sealed(store, "sk-ant-oat01-subscription-bearer", slot="claude_token"),
            _sealed(store, "sk-ant-api03-a-real-api-key", slot="claude_key"),
        ]
    )

    assert refused == []
    assert stream.plaintext == {(str(CONTRACT), "claude_key"): "sk-ant-api03-a-real-api-key"}
    assert [opened.slot for opened in stream.take_opened()] == ["claude_key"]
    assert SETUP_TOKEN_REFUSED_RULE in caplog.text
    assert "sk-ant-oat01" not in caplog.text
    # Not re-opened on the next beat: the same version stays refused, quietly.
    caplog.clear()
    stream.apply([_sealed(store, "sk-ant-oat01-subscription-bearer", slot="claude_token")])
    assert SETUP_TOKEN_REFUSED_RULE not in caplog.text


# ------------------------------------------------------------------ reserved env


@pytest.mark.parametrize("name", ["CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN"])
def test_a_claude_bearer_is_reserved_from_a_runner_hook(name: str) -> None:
    assert name in RESERVED_DIRECTIVE_ENV


def test_a_hook_cannot_put_a_claude_oauth_token_on_a_directive() -> None:
    env = apply_extra_env({}, [("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-x"), ("SAFE", "1")])

    assert env == {"SAFE": "1"}
