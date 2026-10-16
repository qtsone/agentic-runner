"""Where a subscription runs; Codex API-key mode (local-agents 04, QTS-1450).

The Runner chooses each Directive's auth mode itself, from what it can check -- its
registered host party, whether a key is present, whether the Contract runs under its own
uid and whether its harness root holds a login -- never from the payload, never from how
it was installed, and refuses what it cannot verify. These are the unit-level halves; the
activity-level ones (Evidence, residue, the per-Directive wiring) are in
``tests/integration/test_runner_auth_mode_directives.py``.
"""

from __future__ import annotations

import itertools
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest

from agentic_runner.auth_mode import (
    HOST_KEY_NOT_HELD_RULE,
    LLM_ACCESS_UNKNOWN_RULE,
    LLM_KEY_UNSUPPORTED_RULE,
    NO_RUNTIME_SUBSCRIPTION_RULE,
    SETUP_TOKEN_REFUSED_RULE,
    SUBSCRIPTION_NEEDS_CONTRACT_UID_RULE,
    SUBSCRIPTION_NEEDS_HARNESS_LOGIN_RULE,
    SUBSCRIPTION_NOT_SIGNED_IN_RULE,
    SUBSCRIPTION_OFF_PERSONAL_RUNNER_RULE,
    AuthModeRefusedError,
    choose_auth_mode,
    require_named_access,
)
from agentic_runner.llm_proxy import CredentialSlot, LlmProxy, SlotStore
from agentic_runner.sealed_box import (
    RecipientKeyStore,
    SealedCredentialStream,
    seal,
)
from agentic_runner.workers._runtime_support import RESERVED_DIRECTIVE_ENV, apply_extra_env
from agentic_runner.workers.acp_runtime import AcpRuntime
from agentic_runner.workers.agent_runtime import AuthMode, DirectiveRequest
from agentic_runner.workers.claude_runtime import ClaudeRuntime
from agentic_runner.workers.codex_runtime import CodexRuntime, SubprocessResult
from agentic_runner.workers.command_policy import CommandPolicy, evaluate_command_policy
from agentic_runner.workers.settings import WorkerSettings
from agentic_runner_contracts.llm_access import LlmAccess
from agentic_runner_contracts.runner_registration import SubscriptionState
from agentic_runner_contracts.sealed_credential import SealedCredential, delivery_binding

CONTRACT = UUID("11111111-1111-4111-8111-111111111111")
CODEX_MODES = CodexRuntime.auth_modes
CLAUDE_MODES = ClaudeRuntime.auth_modes
ACP_MODES = AcpRuntime.auth_modes
KEY_ONLY_MODES = frozenset({AuthMode.API_KEY})


# ------------------------------------------------------------------ the mode table

# host party x key present x Contract uid x harness login x runtime -> the mode, or the
# rule that refuses it.
UID, NO_UID, LOGIN, NO_LOGIN = True, False, True, False
MODE_TABLE: list[tuple[str | None, bool, bool, bool, str, frozenset[AuthMode], AuthMode | str]] = [
    # A key always wins, wherever the Runner is.
    ("organisation", True, NO_UID, NO_LOGIN, "codex_cli", CODEX_MODES, AuthMode.API_KEY),
    ("organisation", True, UID, LOGIN, "codex_cli", CODEX_MODES, AuthMode.API_KEY),
    ("account", True, NO_UID, NO_LOGIN, "claude_code", CLAUDE_MODES, AuthMode.API_KEY),
    ("user", True, NO_UID, NO_LOGIN, "codex_cli", CODEX_MODES, AuthMode.API_KEY),
    ("user", True, NO_UID, NO_LOGIN, "claude_code", CLAUDE_MODES, AuthMode.API_KEY),
    # A shared Runner -- a customer-operated cluster -- runs a subscription only with both
    # a Contract uid and a login in that Contract's harness root (QTS-1450).
    ("organisation", False, UID, LOGIN, "codex_cli", CODEX_MODES, AuthMode.SUBSCRIPTION),
    ("account", False, UID, LOGIN, "claude_code", ACP_MODES, AuthMode.SUBSCRIPTION),
    (
        "organisation",
        False,
        NO_UID,
        LOGIN,
        "codex_cli",
        CODEX_MODES,
        SUBSCRIPTION_NEEDS_CONTRACT_UID_RULE,
    ),
    (
        "account",
        False,
        NO_UID,
        NO_LOGIN,
        "claude_code",
        ACP_MODES,
        SUBSCRIPTION_NEEDS_CONTRACT_UID_RULE,
    ),
    (
        "organisation",
        False,
        UID,
        NO_LOGIN,
        "codex_cli",
        CODEX_MODES,
        SUBSCRIPTION_NEEDS_HARNESS_LOGIN_RULE,
    ),
    (
        "account",
        False,
        UID,
        NO_LOGIN,
        "claude_code",
        ACP_MODES,
        SUBSCRIPTION_NEEDS_HARNESS_LOGIN_RULE,
    ),
    # The default Claude Code runtime runs a subscription too (local-agents 07, QTS-1737).
    ("organisation", False, UID, LOGIN, "claude_code", CLAUDE_MODES, AuthMode.SUBSCRIPTION),
    # A runtime with no subscription mode has nothing to run on without a key.
    (
        "organisation",
        False,
        UID,
        LOGIN,
        "claude_code",
        KEY_ONLY_MODES,
        NO_RUNTIME_SUBSCRIPTION_RULE,
    ),
    # The person's own Runner: routing tied the Contract to the person, whatever its uids.
    ("user", False, NO_UID, NO_LOGIN, "codex_cli", CODEX_MODES, AuthMode.SUBSCRIPTION),
    ("user", False, NO_UID, NO_LOGIN, "claude_code", CLAUDE_MODES, AuthMode.SUBSCRIPTION),
    ("user", False, NO_UID, NO_LOGIN, "claude_code", KEY_ONLY_MODES, AuthMode.API_KEY),
    # An unregistered process is nobody's own Runner and makes no routing assertion.
    (None, False, UID, LOGIN, "codex_cli", CODEX_MODES, AuthMode.API_KEY),
    (None, True, NO_UID, NO_LOGIN, "codex_cli", CODEX_MODES, AuthMode.API_KEY),
]


@pytest.mark.parametrize(
    ("host_party", "key_present", "contract_uid", "harness_login", "cli_kind", "modes", "expected"),
    MODE_TABLE,
    ids=[
        f"{row[0]}-{'key' if row[1] else 'nokey'}-{'uid' if row[2] else 'nouid'}"
        f"-{'login' if row[3] else 'nologin'}-{row[4]}-{len(row[5])}modes"
        for row in MODE_TABLE
    ],
)
def test_the_mode_is_chosen_from_what_the_runner_can_verify(
    host_party: str | None,
    key_present: bool,
    contract_uid: bool,
    harness_login: bool,
    cli_kind: str,
    modes: frozenset[AuthMode],
    expected: AuthMode | str,
) -> None:
    def choose() -> AuthMode:
        return choose_auth_mode(
            host_party=host_party,
            key_present=key_present,
            runtime_modes=modes,
            contract_uid=contract_uid,
            harness_login=harness_login,
        )

    if isinstance(expected, AuthMode):
        assert choose() == expected
        return
    with pytest.raises(AuthModeRefusedError) as refused:
        choose()
    assert refused.value.rule == expected


def test_a_shared_runner_never_chooses_a_subscription_it_cannot_verify() -> None:
    for host_party in ("organisation", "account"):
        for key_present, contract_uid, harness_login in itertools.product((True, False), repeat=3):
            if contract_uid and harness_login:
                continue
            try:
                mode = choose_auth_mode(
                    host_party=host_party,
                    key_present=key_present,
                    runtime_modes=CODEX_MODES,
                    contract_uid=contract_uid,
                    harness_login=harness_login,
                )
            except AuthModeRefusedError:
                continue
            assert mode == AuthMode.API_KEY


# ------------------------------------------------------------------ the named access (rev 3)

SIGNED_IN = SubscriptionState.SIGNED_IN

NAMED_TABLE: list[tuple[str, str | None, bool, SubscriptionState, frozenset[AuthMode], Any]] = [
    # host_key: the store's key, on any host, or a refusal -- never a subscription instead.
    ("host_key", "organisation", True, SubscriptionState.NONE, CODEX_MODES, AuthMode.API_KEY),
    ("host_key", "user", True, SIGNED_IN, CODEX_MODES, AuthMode.API_KEY),
    ("host_key", "user", False, SIGNED_IN, CODEX_MODES, HOST_KEY_NOT_HELD_RULE),
    # subscription: the person's own Runner, signed in, on a runtime that runs one.
    ("subscription", "user", True, SIGNED_IN, CODEX_MODES, AuthMode.SUBSCRIPTION),
    ("subscription", "organisation", True, SIGNED_IN, CODEX_MODES, "subscription_off"),
    ("subscription", "account", False, SIGNED_IN, CODEX_MODES, "subscription_off"),
    ("subscription", "user", True, SIGNED_IN, KEY_ONLY_MODES, NO_RUNTIME_SUBSCRIPTION_RULE),
    ("subscription", "user", True, SubscriptionState.NONE, CODEX_MODES, "not_signed_in"),
    ("subscription", "user", True, SubscriptionState.EXPIRED, CODEX_MODES, "not_signed_in"),
    ("subscription", "user", True, SubscriptionState.AT_LIMIT, CODEX_MODES, "not_signed_in"),
    # llm_key: the pool's stored keys arrive with T7; until then the Runner fails closed.
    ("llm_key", "organisation", True, SubscriptionState.NONE, CODEX_MODES, "llm_key"),
    ("pigeon", "user", True, SIGNED_IN, CODEX_MODES, LLM_ACCESS_UNKNOWN_RULE),
]
_NAMED_RULES = {
    "subscription_off": SUBSCRIPTION_OFF_PERSONAL_RUNNER_RULE,
    "not_signed_in": SUBSCRIPTION_NOT_SIGNED_IN_RULE,
    "llm_key": LLM_KEY_UNSUPPORTED_RULE,
}


@pytest.mark.parametrize(
    ("kind", "host_party", "host_key_held", "subscription", "modes", "expected"), NAMED_TABLE
)
def test_a_directive_runs_on_exactly_the_access_it_was_routed_with(
    kind: str,
    host_party: str | None,
    host_key_held: bool,
    subscription: SubscriptionState,
    modes: frozenset[AuthMode],
    expected: AuthMode | str,
) -> None:
    """Rev 3: the Runner uses only the access `llm_access` names and fails the Directive
    closed when it no longer holds it -- a held key never stands in for a lost sign-in,
    nor a sign-in for a lost key."""

    def require() -> AuthMode:
        return require_named_access(
            LlmAccess(kind=kind, llm_key_id="key-1" if kind == "llm_key" else ""),
            host_party=host_party,
            host_key_held=host_key_held,
            subscription=subscription,
            runtime_modes=modes,
        )

    if isinstance(expected, AuthMode):
        assert require() == expected
        return
    with pytest.raises(AuthModeRefusedError) as refused:
        require()
    assert refused.value.rule == _NAMED_RULES.get(expected, expected)


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
