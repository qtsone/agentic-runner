"""OAuth Connectors on the Runner, against a fake provider (console-v2 issue 30).

Driven through ``ControlPlaneStream`` and a fake platform that does only what slice 24's
platform does: relay ack items, store each slot's token ciphertext, hand it back on every
ack. So what is asserted about envelopes is asserted about the real wire.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import pytest

from agentic_runner import service
from agentic_runner.credentials import (
    CredentialResolver,
    EmptyCredentialStore,
    FakeCredentialStore,
    HostCredentialStore,
)
from agentic_runner.integrations.oauth.fake_provider import FakeOAuthProvider
from agentic_runner.llm_proxy import CeilingStore, LlmProxy, SlotStore, UsageOutbox
from agentic_runner.oauth_connectors import TOKEN_SET_PREFIX, OAuthConnectors
from agentic_runner.registration import RunnerState
from agentic_runner.sealed_box import (
    AUTHORED_PREFIX,
    RecipientKeyPair,
    RecipientKeyStore,
    SealedCredentialStream,
    generate_recipient_key,
    seal,
)
from agentic_runner_contracts import __version__ as contracts_version
from agentic_runner_contracts.oauth_connector import (
    OAuthClient,
    OAuthDisconnect,
    OAuthOutcome,
    OAuthOutcomeKind,
    OAuthStart,
    SealedOAuthCode,
    oauth_code_binding,
)
from agentic_runner_contracts.runner_registration import (
    FloorState,
    HeartbeatAck,
    HeartbeatEnvelope,
)
from agentic_runner_contracts.sealed_credential import SealedCredential, delivery_binding

CONTRACT = UUID("11111111-1111-4111-8111-111111111111")
SLOT = "LINEAR_TOKEN"
SECRET_SLOT = "LINEAR_CLIENT_SECRET"
REDIRECT = "https://agentic.example/connectors/oauth/callback"
AUTHORIZATION = "oa_" + "a" * 32
T0 = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


class _Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


class _Platform:
    """Slice 24's platform, as far as a Runner can see it."""

    server_date = None

    def __init__(self) -> None:
        self.sent: list[HeartbeatEnvelope] = []
        self.acks: list[HeartbeatAck] = []
        self.slots: dict[tuple[UUID, str], SealedCredential] = {}
        self.queued: dict[str, list[Any]] = {}

    async def heartbeat(self, state: Any, envelope: HeartbeatEnvelope) -> HeartbeatAck:
        self.sent.append(envelope)
        for upload in [*envelope.oauth_tokens, *envelope.resealed]:
            held = self.slots.get(upload.key)
            # First upload of a version wins: a second Runner's refresh to the same
            # version is not stored, and that Runner adopts this one from the ack.
            if held is None or upload.version > held.version:
                self.slots[upload.key] = upload
        ack = HeartbeatAck(
            runner_id=uuid4(),
            floor_state=FloorState.OK,
            contracts_floor=contracts_version,
            tag_set_version=1,
            accepts_new_directives=True,
            sealed_credentials=list(self.slots.values()),
            **self.queued,
        )
        self.queued = {}
        self.acks.append(ack)
        return ack


def _client(
    provider: FakeOAuthProvider, *, secret: bool = False, revoke: bool = True
) -> OAuthClient:
    return OAuthClient(
        authorize_endpoint=provider.authorize_endpoint,
        token_endpoint=provider.token_endpoint,
        revocation_endpoint=provider.revocation_endpoint if revoke else None,
        client_id=provider.client_id,
        client_secret_slot=SECRET_SLOT if secret else None,
        scopes=["read", "write"],
    )


def _runner(
    tmp_path: Path,
    name: str,
    platform: _Platform,
    provider: FakeOAuthProvider,
    *,
    key: RecipientKeyPair,
    clock: _Clock,
    store: HostCredentialStore | None = None,
) -> service.ControlPlaneStream:
    keys = RecipientKeyStore(tmp_path / name)
    keys.install(key, managed_by="helm", now=T0)
    sealed = SealedCredentialStream(keys)
    credentials = CredentialResolver(store=store or EmptyCredentialStore())
    runner_id = uuid4()
    return service.ControlPlaneStream(
        client=platform,  # type: ignore[arg-type]
        state=RunnerState(
            runner_id=runner_id,
            identity_id="id",
            private_key_pem="unused",
            temporal_namespace="org-x",
            task_queue=f"runner.{runner_id}",
        ),
        isolation=service.IsolationMode.NONE,
        proxy=LlmProxy(slots=SlotStore(), ceilings=CeilingStore(), outbox=UsageOutbox()),
        sealed=sealed,
        credentials=credentials,
        load=service._LoadInterceptor(),
        clock=clock,
        oauth=OAuthConnectors(
            sealed=sealed, provider=provider, credentials=credentials, clock=clock
        ),
    )


def _sealed_code(
    key: RecipientKeyPair, code: str, state: str, *, authorization_id: str = AUTHORIZATION
) -> SealedOAuthCode:
    """What the callback page posts: the code sealed in the browser, ``state`` beside it."""

    return SealedOAuthCode(
        contract_id=CONTRACT,
        authorization_id=authorization_id,
        recipient_key_id=key.key_id,
        state=state,
        ciphertext=seal(
            public_key=key.public_key,
            binding=oauth_code_binding(
                contract_id=CONTRACT, authorization_id=authorization_id, recipient_key_id=key.key_id
            ),
            plaintext=code,
        ),
    )


def _funder_delivery(
    key: RecipientKeyPair, value: str, *, slot: str = SLOT, version: int = 1
) -> SealedCredential:
    """What any funder can send: a value sealed to the installation's public key."""

    return SealedCredential(
        contract_id=CONTRACT,
        slot=slot,
        recipient_key_id=key.key_id,
        version=version,
        ciphertext=seal(
            public_key=key.public_key,
            binding=delivery_binding(contract_id=CONTRACT, slot=slot, recipient_key_id=key.key_id),
            plaintext=value,
        ),
    )


def _outcomes(platform: _Platform) -> list[OAuthOutcome]:
    return [outcome for envelope in platform.sent for outcome in envelope.oauth_outcomes]


def _served(runner: service.ControlPlaneStream) -> str:
    return runner.credentials.resolve(
        contract_id=str(CONTRACT), manifest=[SLOT]
    ).for_runner_hosted_server(SLOT)


async def _start(
    runner: service.ControlPlaneStream, platform: _Platform, client: OAuthClient
) -> str:
    platform.queued = {
        "oauth_starts": [
            OAuthStart(
                contract_id=CONTRACT,
                slot=SLOT,
                authorization_id=AUTHORIZATION,
                client=client,
                redirect_uri=REDIRECT,
            )
        ]
    }
    await runner.exchange([])
    await runner.exchange([])
    (authorization,) = platform.sent[-1].oauth_authorizations
    assert authorization.authorization_id == AUTHORIZATION
    return authorization.authorize_url


async def _connect(
    runner: service.ControlPlaneStream,
    platform: _Platform,
    provider: FakeOAuthProvider,
    key: RecipientKeyPair,
    client: OAuthClient | None = None,
) -> None:
    url = await _start(runner, platform, client or _client(provider))
    code, state = provider.consent(url)
    platform.queued = {"oauth_codes": [_sealed_code(key, code, state)]}
    await runner.exchange([])  # the exchange runs on this ack
    await runner.exchange([])  # the ciphertext and the outcome ride this envelope
    await runner.exchange([])  # and the platform's stored copy comes back on this ack


@pytest.fixture
def provider() -> FakeOAuthProvider:
    return FakeOAuthProvider()


@pytest.fixture
def key() -> RecipientKeyPair:
    return generate_recipient_key()


@pytest.mark.asyncio
async def test_the_authorize_url_carries_the_challenge_and_state_but_not_the_verifier(
    tmp_path: Path, provider: FakeOAuthProvider, key: RecipientKeyPair
) -> None:
    platform = _Platform()
    runner = _runner(tmp_path, "a", platform, provider, key=key, clock=_Clock())

    url = await _start(runner, platform, _client(provider))

    parts = urlsplit(url)
    query = {name: values[0] for name, values in parse_qs(parts.query).items()}
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == provider.authorize_endpoint
    assert query["response_type"] == "code"
    assert query["client_id"] == provider.client_id
    assert query["redirect_uri"] == REDIRECT
    assert query["scope"] == "read write"
    assert query["code_challenge_method"] == "S256"
    assert len(query["code_challenge"]) == 43 and len(query["state"]) >= 43
    assert "code_verifier" not in query


@pytest.mark.asyncio
async def test_a_connect_seals_both_tokens_and_returns_ciphertext_only(
    tmp_path: Path,
    provider: FakeOAuthProvider,
    key: RecipientKeyPair,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    platform = _Platform()
    runner = _runner(tmp_path, "a", platform, provider, key=key, clock=_Clock())

    await _connect(runner, platform, provider, key)

    assert [outcome.outcome for outcome in _outcomes(platform)] == [OAuthOutcomeKind.CONNECTED]
    stored = platform.slots[(CONTRACT, SLOT)]
    assert (stored.version, stored.recipient_key_id) == (1, key.key_id)
    access, refresh = provider.issued
    assert _served(runner) == access
    assert provider.access_valid(access)
    (exchange_url, form) = provider.requests[0]
    verifier = form["code_verifier"]
    code = form["code"]
    observed = [
        caplog.text,
        *(envelope.model_dump_json() for envelope in platform.sent),
        *(ack.model_dump_json() for ack in platform.acks),
    ]
    for text in observed:
        for secret in (verifier, code, access, refresh):
            assert secret not in text


@pytest.mark.asyncio
async def test_a_wrong_state_is_refused_and_the_authorisation_keeps_waiting(
    tmp_path: Path, provider: FakeOAuthProvider, key: RecipientKeyPair
) -> None:
    platform = _Platform()
    runner = _runner(tmp_path, "a", platform, provider, key=key, clock=_Clock())
    code, state = provider.consent(await _start(runner, platform, _client(provider)))

    platform.queued = {"oauth_codes": [_sealed_code(key, code, "forged-" + state)]}
    await runner.exchange([])
    await runner.exchange([])

    assert [outcome.outcome for outcome in _outcomes(platform)] == [OAuthOutcomeKind.STATE_MISMATCH]
    assert provider.requests == [], "a code with the wrong state is never spent"

    platform.queued = {"oauth_codes": [_sealed_code(key, code, state)]}
    await runner.exchange([])
    await runner.exchange([])
    assert _outcomes(platform)[-1].outcome is OAuthOutcomeKind.CONNECTED


@pytest.mark.asyncio
async def test_a_replayed_code_is_refused_without_reaching_the_provider(
    tmp_path: Path, provider: FakeOAuthProvider, key: RecipientKeyPair
) -> None:
    platform = _Platform()
    runner = _runner(tmp_path, "a", platform, provider, key=key, clock=_Clock())
    code, state = provider.consent(await _start(runner, platform, _client(provider)))
    replay = _sealed_code(key, code, state)

    platform.queued = {"oauth_codes": [replay]}
    await runner.exchange([])
    platform.queued = {"oauth_codes": [replay]}
    await runner.exchange([])
    await runner.exchange([])

    assert [outcome.outcome for outcome in _outcomes(platform)] == [
        OAuthOutcomeKind.CONNECTED,
        OAuthOutcomeKind.UNKNOWN,
    ]
    assert len(provider.requests) == 1


@pytest.mark.asyncio
async def test_a_code_bound_to_another_authorisation_or_contract_does_not_open(
    tmp_path: Path, provider: FakeOAuthProvider, key: RecipientKeyPair
) -> None:
    platform = _Platform()
    runner = _runner(tmp_path, "a", platform, provider, key=key, clock=_Clock())
    code, state = provider.consent(await _start(runner, platform, _client(provider)))
    lifted = _sealed_code(key, code, state, authorization_id="oa_" + "b" * 32)
    lifted = lifted.model_copy(update={"authorization_id": AUTHORIZATION})
    other_contract = _sealed_code(key, code, state).model_copy(update={"contract_id": uuid4()})

    platform.queued = {"oauth_codes": [lifted, other_contract]}
    await runner.exchange([])
    await runner.exchange([])

    assert [outcome.outcome for outcome in _outcomes(platform)] == [
        OAuthOutcomeKind.UNOPENABLE,
        OAuthOutcomeKind.UNKNOWN,
    ]
    assert provider.requests == []


@pytest.mark.asyncio
async def test_an_authorisation_past_its_window_forgets_the_verifier(
    tmp_path: Path, provider: FakeOAuthProvider, key: RecipientKeyPair
) -> None:
    clock = _Clock()
    platform = _Platform()
    runner = _runner(tmp_path, "a", platform, provider, key=key, clock=clock)
    code, state = provider.consent(await _start(runner, platform, _client(provider)))

    clock.now += timedelta(minutes=11)
    await runner.exchange([])
    platform.queued = {"oauth_codes": [_sealed_code(key, code, state)]}
    await runner.exchange([])
    await runner.exchange([])

    assert [outcome.outcome for outcome in _outcomes(platform)] == [
        OAuthOutcomeKind.EXPIRED,
        OAuthOutcomeKind.UNKNOWN,
    ]
    assert provider.requests == []


@pytest.mark.asyncio
async def test_the_client_secret_comes_from_its_credential_reference_slot(
    tmp_path: Path, key: RecipientKeyPair
) -> None:
    provider = FakeOAuthProvider(client_secret="s3cret-client")
    platform = _Platform()
    runner = _runner(tmp_path, "a", platform, provider, key=key, clock=_Clock())
    client = _client(provider, secret=True)

    code, state = provider.consent(await _start(runner, platform, client))
    platform.queued = {"oauth_codes": [_sealed_code(key, code, state)]}
    await runner.exchange([])
    await runner.exchange([])
    assert _outcomes(platform)[-1].outcome is OAuthOutcomeKind.CLIENT_SECRET_MISSING

    platform.slots[(CONTRACT, SECRET_SLOT)] = SealedCredential(
        contract_id=CONTRACT,
        slot=SECRET_SLOT,
        recipient_key_id=key.key_id,
        version=1,
        ciphertext=seal(
            public_key=key.public_key,
            binding=delivery_binding(
                contract_id=CONTRACT, slot=SECRET_SLOT, recipient_key_id=key.key_id
            ),
            plaintext="s3cret-client",
        ),
    )
    await _connect(runner, platform, provider, key, client)

    assert _outcomes(platform)[-1].outcome is OAuthOutcomeKind.CONNECTED
    assert provider.requests[-1][1]["client_secret"] == "s3cret-client"
    for envelope in platform.sent:
        assert "s3cret-client" not in envelope.model_dump_json()


@pytest.mark.asyncio
async def test_refresh_before_expiry_reseals_and_uploads_the_new_ciphertext(
    tmp_path: Path, provider: FakeOAuthProvider, key: RecipientKeyPair
) -> None:
    clock = _Clock()
    platform = _Platform()
    runner = _runner(tmp_path, "a", platform, provider, key=key, clock=clock)
    await _connect(runner, platform, provider, key)
    first = platform.slots[(CONTRACT, SLOT)]
    old_access, old_refresh = provider.issued

    clock.now += timedelta(minutes=50)
    await runner.exchange([])
    assert platform.slots[(CONTRACT, SLOT)] == first, "not yet within the margin"

    clock.now += timedelta(minutes=6)
    await runner.exchange([])

    refreshed = platform.slots[(CONTRACT, SLOT)]
    assert refreshed.version == 2 and refreshed.ciphertext != first.ciphertext
    grant = provider.requests[-1][1]
    assert (grant["grant_type"], grant["refresh_token"]) == ("refresh_token", old_refresh)
    new_access = provider.issued[2]
    assert _served(runner) == new_access
    assert _outcomes(platform)[-1].outcome is OAuthOutcomeKind.REFRESHED
    assert all(new_access not in envelope.model_dump_json() for envelope in platform.sent)
    assert old_access not in _served(runner)


@pytest.mark.asyncio
async def test_a_401_refreshes_now_and_an_upload_lost_with_its_beat_is_resent(
    tmp_path: Path, provider: FakeOAuthProvider, key: RecipientKeyPair
) -> None:
    platform = _Platform()
    runner = _runner(tmp_path, "a", platform, provider, key=key, clock=_Clock())
    await _connect(runner, platform, provider, key)
    assert runner.oauth is not None

    assert await runner.oauth.refresh(CONTRACT, SLOT) is True

    lost = platform.heartbeat
    attempts: list[HeartbeatEnvelope] = []

    async def failing(state: Any, envelope: HeartbeatEnvelope) -> HeartbeatAck:
        attempts.append(envelope)
        raise ConnectionError("beat lost")

    platform.heartbeat = failing  # type: ignore[method-assign]
    with pytest.raises(ConnectionError):
        await runner.exchange([])
    platform.heartbeat = lost  # type: ignore[method-assign]
    await runner.exchange([])

    assert [upload.version for upload in attempts[0].oauth_tokens] == [2]
    assert platform.slots[(CONTRACT, SLOT)].version == 2
    assert _served(runner) == provider.issued[2]


@pytest.mark.asyncio
async def test_a_failed_refresh_is_reported_and_not_retried_every_beat(
    tmp_path: Path, provider: FakeOAuthProvider, key: RecipientKeyPair
) -> None:
    clock = _Clock()
    platform = _Platform()
    runner = _runner(tmp_path, "a", platform, provider, key=key, clock=clock)
    await _connect(runner, platform, provider, key)
    provider._refresh.clear()  # the provider forgot the grant

    clock.now += timedelta(minutes=57)
    await runner.exchange([])
    await runner.exchange([])
    await runner.exchange([])

    failed = [o for o in _outcomes(platform) if o.outcome is OAuthOutcomeKind.REFRESH_FAILED]
    assert len(failed) == 1
    assert failed[0].provider_error == "invalid_grant"
    refreshes = [form for _, form in provider.requests if form["grant_type"] == "refresh_token"]
    assert len(refreshes) == 1


@pytest.mark.asyncio
async def test_a_second_runner_of_the_installation_opens_the_refreshed_ciphertext(
    tmp_path: Path, provider: FakeOAuthProvider, key: RecipientKeyPair
) -> None:
    """Two replicas share one Recipient Key; a Runner of another installation does not."""

    platform = _Platform()
    first = _runner(tmp_path, "a", platform, provider, key=key, clock=_Clock())
    second = _runner(tmp_path, "b", platform, provider, key=key, clock=_Clock())
    elsewhere = _runner(
        tmp_path, "c", platform, provider, key=generate_recipient_key(), clock=_Clock()
    )
    await _connect(first, platform, provider, key)
    assert first.oauth is not None
    await first.oauth.refresh(CONTRACT, SLOT)
    await first.exchange([])

    await second.exchange([])
    await elsewhere.exchange([])

    assert platform.slots[(CONTRACT, SLOT)].version == 2
    assert _served(second) == provider.issued[2]
    assert elsewhere.sealed.plaintext == {}
    with pytest.raises(Exception, match="not_installed"):
        _served(elsewhere)


@pytest.mark.asyncio
async def test_the_second_runner_refreshes_from_what_it_opened(
    tmp_path: Path, provider: FakeOAuthProvider, key: RecipientKeyPair
) -> None:
    """The token set carries the token endpoint and the client, so any Runner of the
    installation can refresh -- not only the one that connected."""

    platform = _Platform()
    first = _runner(tmp_path, "a", platform, provider, key=key, clock=_Clock())
    second = _runner(tmp_path, "b", platform, provider, key=key, clock=_Clock())
    await _connect(first, platform, provider, key)
    await second.exchange([])
    assert second.oauth is not None

    assert await second.oauth.refresh(CONTRACT, SLOT) is True
    await second.exchange([])
    await first.exchange([])

    assert platform.slots[(CONTRACT, SLOT)].version == 2
    assert _served(first) == _served(second) == provider.issued[2]


@pytest.mark.asyncio
async def test_disconnect_revokes_at_the_provider_and_drops_the_plaintext(
    tmp_path: Path, provider: FakeOAuthProvider, key: RecipientKeyPair
) -> None:
    platform = _Platform()
    runner = _runner(tmp_path, "a", platform, provider, key=key, clock=_Clock())
    await _connect(runner, platform, provider, key)
    access, refresh = provider.issued

    del platform.slots[(CONTRACT, SLOT)]
    platform.queued = {
        "oauth_disconnects": [
            OAuthDisconnect(contract_id=CONTRACT, slot=SLOT, client=_client(provider))
        ]
    }
    await runner.exchange([])
    await runner.exchange([])

    url, form = provider.requests[-1]
    assert url == provider.revocation_endpoint
    assert (form["token"], form["token_type_hint"]) == (refresh, "refresh_token")
    assert not provider.access_valid(access)
    assert _outcomes(platform)[-1].outcome is OAuthOutcomeKind.REVOKED
    assert runner.sealed.plaintext == {}
    with pytest.raises(Exception, match="not_installed"):
        _served(runner)


@pytest.mark.asyncio
async def test_disconnect_with_no_revocation_endpoint_drops_and_says_so(
    tmp_path: Path, provider: FakeOAuthProvider, key: RecipientKeyPair
) -> None:
    platform = _Platform()
    runner = _runner(tmp_path, "a", platform, provider, key=key, clock=_Clock())
    await _connect(runner, platform, provider, key)
    calls = len(provider.requests)

    # The platform has not deleted the ciphertext yet: the stale copy is not re-adopted.
    platform.queued = {
        "oauth_disconnects": [
            OAuthDisconnect(contract_id=CONTRACT, slot=SLOT, client=_client(provider, revoke=False))
        ]
    }
    await runner.exchange([])
    await runner.exchange([])

    assert _outcomes(platform)[-1].outcome is OAuthOutcomeKind.DROPPED
    assert len(provider.requests) == calls
    with pytest.raises(Exception, match="not_installed"):
        _served(runner)


@pytest.mark.asyncio
async def test_a_beat_with_nothing_to_say_omits_every_oauth_key(
    tmp_path: Path, provider: FakeOAuthProvider, key: RecipientKeyPair
) -> None:
    """A control plane on contracts 3.7 forbids extra keys."""

    platform = _Platform()
    runner = _runner(tmp_path, "a", platform, provider, key=key, clock=_Clock())

    await runner.exchange([])

    body = json.loads(platform.sent[0].model_dump_json())
    assert not {"oauth_authorizations", "oauth_tokens", "oauth_outcomes"} & set(body)


@pytest.mark.asyncio
@pytest.mark.parametrize("forgery", ["bare", "bad_tag"])
async def test_a_funder_delivered_token_set_is_served_as_delivered_and_never_refreshed(
    tmp_path: Path, provider: FakeOAuthProvider, key: RecipientKeyPair, forgery: str
) -> None:
    """Sealing is public-key: a funder can put a token-set-shaped value in the slot. It
    must not steer the Runner into sending the host's client secret anywhere."""

    clock = _Clock()
    platform = _Platform()
    host = FakeCredentialStore({SECRET_SLOT: "host-owned-secret"})
    runner = _runner(tmp_path, "a", platform, provider, key=key, clock=clock, store=host)
    body = json.dumps(
        {
            "access_token": "funder-access",
            "refresh_token": "funder-refresh",
            "expires_at": T0.isoformat(),
            "token_endpoint": provider.token_endpoint,
            "client_id": provider.client_id,
            "client_secret_slot": SECRET_SLOT,
        }
    )
    value = TOKEN_SET_PREFIX + body
    if forgery == "bad_tag":
        value = f"{AUTHORED_PREFIX}{'A' * 43}=:{value}"
    platform.slots[(CONTRACT, SLOT)] = _funder_delivery(key, value)
    assert runner.oauth is not None

    await runner.exchange([])
    clock.now += timedelta(hours=1)
    await runner.exchange([])
    refreshed = await runner.oauth.refresh(CONTRACT, SLOT)
    await runner.exchange([])

    assert refreshed is False
    assert provider.requests == []
    assert _served(runner) == value
    assert _outcomes(platform) == []
    assert all(envelope.oauth_tokens == [] for envelope in platform.sent)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        "{",
        "[]",
        '{"access_token":"a"}',
        json.dumps(
            {
                "access_token": "a",
                "refresh_token": "r",
                "expires_at": None,
                "token_endpoint": "http://plain.example/token",
                "client_id": "c",
                "client_secret_slot": None,
            }
        ),
        json.dumps(
            {
                "access_token": 1,
                "refresh_token": "r",
                "expires_at": None,
                "token_endpoint": "https://idp.example/token",
                "client_id": "c",
                "client_secret_slot": None,
            }
        ),
    ],
    ids=["truncated", "not_an_object", "missing_keys", "http_endpoint", "wrong_type"],
)
async def test_an_unreadable_token_set_costs_its_slot_and_not_the_beat(
    tmp_path: Path, provider: FakeOAuthProvider, key: RecipientKeyPair, body: str
) -> None:
    platform = _Platform()
    runner = _runner(tmp_path, "a", platform, provider, key=key, clock=_Clock())
    platform.slots[(CONTRACT, SLOT)] = runner.sealed.seal_authored(
        str(CONTRACT), SLOT, version=1, value=TOKEN_SET_PREFIX + body
    )
    platform.slots[(CONTRACT, SECRET_SLOT)] = _funder_delivery(key, "other", slot=SECRET_SLOT)
    assert runner.oauth is not None

    await runner.exchange([])
    await runner.exchange([])

    assert await runner.oauth.refresh(CONTRACT, SLOT) is False
    assert provider.requests == []
    with pytest.raises(Exception, match="not_installed"):
        _served(runner)
    assert (
        runner.credentials.resolve(
            contract_id=str(CONTRACT), manifest=[SECRET_SLOT]
        ).for_runner_hosted_server(SECRET_SLOT)
        == "other"
    )


@pytest.mark.asyncio
async def test_a_token_set_stays_a_token_set_across_a_recipient_key_renewal(
    tmp_path: Path, provider: FakeOAuthProvider, key: RecipientKeyPair
) -> None:
    """The authored tag is keyed off the private half, so the renewal's re-seal re-tags
    it: once the old key is retired, the slot still refreshes rather than serving raw."""

    platform = _Platform()
    runner = _runner(tmp_path, "a", platform, provider, key=key, clock=_Clock())
    await _connect(runner, platform, provider, key)
    keys = runner.sealed._keys  # the installer replaced the Secret (``install``'s helm path)
    fresh = generate_recipient_key()
    keys.install(fresh, managed_by="helm", now=T0)

    await runner.exchange([])  # the ack shows the slot still under the old key
    await runner.exchange([])  # so the renewal re-seals it, and the echo opens under the new

    assert keys.previous() is None
    stored = platform.slots[(CONTRACT, SLOT)]
    assert stored.recipient_key_id == fresh.key_id
    assert _served(runner) == provider.issued[0]
    assert runner.oauth is not None
    assert await runner.oauth.refresh(CONTRACT, SLOT) is True
