"""The User Key on a person's Runner (QTS-2478, Spec: Agent Guardrails rev 7, S3, S5, S6).

What this pins, on ``fixtures/user-key.fixture.json``, which the console's tests read too:

* a User Key wrapped for two passkeys opens with either one's PRF output, and a wrap moved
  onto another passkey, person or User Key does not;
* ``login`` keeps the User Key the browser sealed to this Runner, and a key sealed for
  another person or Recipient Key, or one that is not the key it names, is not kept;
* a member Secret sealed to the User Key opens on a Runner that holds it, with no
  retyping, and on no other; a release asks with every holder key the Runner holds;
* the heartbeat reports the User Key's fingerprint, and says nothing while there is none.
"""

from __future__ import annotations

import base64
import json
from importlib.resources import files
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from agentic_runner import service
from agentic_runner.activities import RunnerRalphActivities, _RuntimeContextState
from agentic_runner.credentials import CredentialResolver, FakeCredentialStore
from agentic_runner.pool_key import LlmKeyOpener, LlmKeyUnopenableError, parse_pool_key
from agentic_runner.sealed_box import RecipientKeyPair, RecipientKeyStore
from agentic_runner.testing import FakeControlPlane
from agentic_runner.user_key import UserKeyError, load_user_key, open_user_key
from agentic_runner_contracts.runner_registration import HeartbeatEnvelope
from agentic_runner_contracts.secret import SecretRelease
from agentic_runner_contracts.user_key import (
    PASSKEY_PRF_INPUT,
    SealedUserKey,
    enrolment_binding,
    passkey_wrap_binding,
)

CONTRACT = "5f0b8c2e-1d3a-4e6f-9a7b-2c4d6e8f0a1b"
CONTROL_PLANE = "http://control-plane.test"


def _fixture() -> dict[str, Any]:
    raw = files("agentic_runner_contracts").joinpath("fixtures/user-key.fixture.json")
    return dict(json.loads(raw.read_text(encoding="utf-8")))


def _unwrap(wrapped: str, prf_output: str, binding: bytes) -> str:
    """The browser's unwrap (``user-key.ts``), in Python: HKDF-SHA256 then AES-256-GCM."""

    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=b"", info=binding).derive(
        base64.b64decode(prf_output)
    )
    blob = base64.b64decode(wrapped)
    return base64.b64encode(AESGCM(key).decrypt(blob[:12], blob[12:], binding)).decode()


def _recipient_keys(tmp_path: Path, fixture: dict[str, Any]) -> RecipientKeyStore:
    keys = RecipientKeyStore(tmp_path)
    keys.install(
        RecipientKeyPair(
            key_id=fixture["recipient_key_id"],
            public_key=fixture["recipient_public_key"],
            private_key=fixture["recipient_private_key"],
        ),
        managed_by="test",
        now=keys.rotated_at(),
    )
    return keys


def _sealed(fixture: dict[str, Any], **changes: str) -> SealedUserKey:
    return SealedUserKey(
        public_key=changes.get("public_key", fixture["user_key_public"]),
        fingerprint=changes.get("fingerprint", fixture["user_key_fingerprint"]),
        recipient_key_id=changes.get("recipient_key_id", fixture["recipient_key_id"]),
        sealed_private=fixture["sealed_private"],
    )


def _release(fixture: dict[str, Any]) -> SecretRelease:
    return SecretRelease.model_validate(
        {
            "organisation_id": fixture["organisation_id"],
            "recipient_key_id": fixture["recipient_key_id"],
            "secrets": [
                {
                    "secret_id": fixture["secret_id"],
                    "name": fixture["name"],
                    "scope": fixture["scope"],
                    "holder_kind": fixture["holder_kind"],
                    "holder_fingerprint": fixture["user_key_fingerprint"],
                    "holder_share": fixture["holder_share"],
                    "platform_share": fixture["platform_share"],
                }
            ],
        }
    )


# ------------------------------------------------------------------ passkeys


def test_the_fixture_s_bindings_are_the_contracts() -> None:
    fixture = _fixture()

    assert fixture["prf_input"].encode() == PASSKEY_PRF_INPUT
    assert fixture["enrolment_binding"].encode() == enrolment_binding(
        user_profile_id=fixture["user_profile_id"],
        user_key_fingerprint=fixture["user_key_fingerprint"],
        recipient_key_id=fixture["recipient_key_id"],
    )
    for passkey in fixture["passkeys"]:
        assert passkey["wrap_binding"].encode() == passkey_wrap_binding(
            user_profile_id=fixture["user_profile_id"],
            user_key_fingerprint=fixture["user_key_fingerprint"],
            credential_id=passkey["credential_id"],
        )


def test_a_user_key_wrapped_for_two_passkeys_opens_with_either() -> None:
    fixture = _fixture()
    first, second = fixture["passkeys"]

    for passkey in (first, second):
        opened = _unwrap(
            passkey["wrapped_private"], passkey["prf_output"], passkey["wrap_binding"].encode()
        )
        assert opened == fixture["user_key_private"]
    # Removing the first passkey deletes only its own wrap: the second needs nothing of it.
    assert (
        _unwrap(second["wrapped_private"], second["prf_output"], second["wrap_binding"].encode())
        == fixture["user_key_private"]
    )


def test_a_wrap_opens_only_with_its_own_passkey_and_binding() -> None:
    fixture = _fixture()
    first, second = fixture["passkeys"]

    with pytest.raises(InvalidTag):
        _unwrap(first["wrapped_private"], second["prf_output"], first["wrap_binding"].encode())
    with pytest.raises(InvalidTag):
        # The first passkey's wrap moved onto the second passkey's row.
        _unwrap(first["wrapped_private"], first["prf_output"], second["wrap_binding"].encode())
    with pytest.raises(InvalidTag):
        another_person = passkey_wrap_binding(
            user_profile_id="00000000-0000-4000-8000-000000000000",
            user_key_fingerprint=fixture["user_key_fingerprint"],
            credential_id=first["credential_id"],
        )
        _unwrap(first["wrapped_private"], first["prf_output"], another_person)


# ------------------------------------------------------------------ enrolment


def test_the_runner_opens_the_user_key_sealed_to_it(tmp_path: Path) -> None:
    fixture = _fixture()

    key = open_user_key(
        _sealed(fixture),
        user_profile_id=fixture["user_profile_id"],
        recipient_keys=_recipient_keys(tmp_path, fixture),
    )

    assert key.private_key == fixture["user_key_private"]
    assert key.fingerprint == fixture["user_key_fingerprint"]


@pytest.mark.parametrize(
    ("person", "changes"),
    [
        ("00000000-0000-4000-8000-000000000000", {}),
        (None, {"recipient_key_id": "rk_another000000000000000000000000"}),
        (None, {"fingerprint": "0000:0000:0000:0000:0000:0000:0000:0000"}),
        (None, {"public_key": base64.b64encode(bytes(32)).decode()}),
    ],
    ids=["another person", "another Recipient Key", "another fingerprint", "another key"],
)
def test_a_user_key_not_sealed_for_this_person_and_runner_is_refused(
    tmp_path: Path, person: str | None, changes: dict[str, str]
) -> None:
    fixture = _fixture()

    with pytest.raises(UserKeyError):
        open_user_key(
            _sealed(fixture, **changes),
            user_profile_id=person or fixture["user_profile_id"],
            recipient_keys=_recipient_keys(tmp_path, fixture),
        )


@pytest.mark.asyncio
async def test_login_keeps_the_user_key_the_browser_sealed_to_the_new_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENTIC_RUNNER_ISOLATION", "none")
    monkeypatch.delenv("AGENTIC_RUNNER_TAGS", raising=False)
    fixture = _fixture()
    plane = FakeControlPlane()
    said: list[str] = []

    async def confirm(_: float) -> None:
        plane.confirm_login(
            "CODE-0001",
            user_key=(
                fixture["user_key_private"],
                fixture["user_key_public"],
                fixture["user_key_fingerprint"],
            ),
        )

    async with httpx.AsyncClient(transport=plane.transport()) as http:
        await service.login(
            state_dir=tmp_path,
            control_plane=CONTROL_PLANE,
            name="laptop",
            say=said.append,
            open_browser=lambda _: None,
            client=http,
            sleep=confirm,
        )

    kept = load_user_key(tmp_path)
    assert kept is not None and kept.fingerprint == fixture["user_key_fingerprint"]
    assert f"user key       {fixture['user_key_fingerprint']}" in said
    # The key the browser sealed to is the one the person was shown beside the code.
    recipient = RecipientKeyStore(tmp_path).current()
    assert f"runner key     {recipient.fingerprint}" in said
    assert (tmp_path / "user-key").stat().st_mode & 0o077 == 0
    assert plane.logins[0].recipient_key.key_id == recipient.key_id


@pytest.mark.asyncio
async def test_login_without_a_user_key_keeps_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENTIC_RUNNER_ISOLATION", "none")
    plane = FakeControlPlane()

    async def confirm(_: float) -> None:
        plane.confirm_login("CODE-0001")

    async with httpx.AsyncClient(transport=plane.transport()) as http:
        await service.login(
            state_dir=tmp_path,
            control_plane=CONTROL_PLANE,
            name="laptop",
            say=lambda _: None,
            open_browser=lambda _: None,
            client=http,
            sleep=confirm,
        )

    assert load_user_key(tmp_path) is None


# ------------------------------------------------------------------ member Secrets


def _opener(tmp_path: Path, fixture: dict[str, Any], *, user_key: bool) -> LlmKeyOpener:
    held = open_user_key(
        _sealed(fixture),
        user_profile_id=fixture["user_profile_id"],
        recipient_keys=_recipient_keys(tmp_path, fixture),
    )
    return LlmKeyOpener(
        pool_key=None,
        recipient_keys=_recipient_keys(tmp_path, fixture),
        user_key=held if user_key else None,
    )


def test_a_member_secret_opens_on_a_runner_holding_their_user_key(tmp_path: Path) -> None:
    fixture = _fixture()

    values = _opener(tmp_path, fixture, user_key=True).open_secrets(
        _release(fixture), names=[fixture["name"]]
    )

    assert values == {fixture["name"]: fixture["value"]}


def test_a_member_secret_does_not_open_without_their_user_key(tmp_path: Path) -> None:
    fixture = _fixture()
    another_person = LlmKeyOpener(
        pool_key=None,
        recipient_keys=_recipient_keys(tmp_path, fixture),
        user_key=parse_pool_key(base64.b64encode(bytes(range(32))).decode()),
    )

    with pytest.raises(LlmKeyUnopenableError):
        _opener(tmp_path, fixture, user_key=False).open_secrets(
            _release(fixture), names=[fixture["name"]]
        )
    with pytest.raises(LlmKeyUnopenableError):
        another_person.open_secrets(_release(fixture), names=[fixture["name"]])


class _ControlPlane:
    def __init__(self, answer: dict[str, Any]) -> None:
        self.answer = answer
        self.asked: list[dict[str, Any]] = []

    async def release_secrets(self, work_record_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.asked.append(payload)
        return self.answer

    async def append_evidence(
        self, work_record_id: str, *, source: str, payload: dict[str, Any]
    ) -> None:
        raise AssertionError(f"no Evidence expected, got {source}: {payload}")


@pytest.mark.asyncio
async def test_a_runner_with_only_a_user_key_asks_for_its_member_secrets(tmp_path: Path) -> None:
    fixture = _fixture()
    control_plane = _ControlPlane(_release(fixture).model_dump(mode="json"))
    activities = RunnerRalphActivities(
        control_plane,  # type: ignore[arg-type]
        credentials=CredentialResolver(store=FakeCredentialStore({})),
        llm_keys=_opener(tmp_path, fixture, user_key=True),
    )
    state = _RuntimeContextState(
        reviewer=None,
        repository="acme/repo",
        verifier_argv=("true",),
        work_branch="agent/work",
        completion_criteria="do the work",
        contract_id=CONTRACT,
        credential_references=(fixture["name"],),
    )

    credentials = await activities._resolve_credentials("wr-1", state, "wr-1:1")

    assert credentials is not None
    assert credentials.for_verb_seam(fixture["name"]) == fixture["value"]
    assert control_plane.asked[0]["holder_fingerprints"] == [fixture["user_key_fingerprint"]]


def test_the_heartbeat_reports_the_user_key_only_when_held() -> None:
    fixture = _fixture()
    base: dict[str, Any] = {"runner_id": "x"}

    without = HeartbeatEnvelope.model_fields["user_key_fingerprint"]
    assert without.default is None
    body = json.loads(
        HeartbeatEnvelope.model_construct(
            **base, user_key_fingerprint=fixture["user_key_fingerprint"]
        ).model_dump_json()
    )
    assert body["user_key_fingerprint"] == fixture["user_key_fingerprint"]
    assert "user_key_fingerprint" not in json.loads(
        HeartbeatEnvelope.model_construct(**base, user_key_fingerprint=None).model_dump_json()
    )
