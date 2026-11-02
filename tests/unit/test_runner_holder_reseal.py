"""An Organisation Runner reseals "Also members' Runners" Secrets to members' User Keys
(QTS-2479, Spec: Agent Guardrails rev 7, A1, S5).

What this pins, on ``fixtures/secret.fixture.json`` (an Organisation Secret sealed to the
Organisation Key) and ``fixtures/user-key.fixture.json`` (a member's User Key):

* the Organisation Runner opens the holder share with the Organisation Key and seals it to
  the member's User Key, bound to the fingerprint it computes itself; the member's Runner
  then opens the Secret with no retyping;
* a share sealed to another Organisation Key, or bound to another Secret, is not resealed,
  and a Runner without the Organisation Key reseals nothing;
* the ack's reseals are answered on the next envelope, once, and an envelope with none
  says nothing, so an older control plane still parses it.
"""

from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from agentic_runner import service
from agentic_runner.credentials import CredentialResolver, EmptyCredentialStore
from agentic_runner.llm_proxy import LlmProxy, SlotStore
from agentic_runner.pool_key import LlmKeyOpener, parse_pool_key
from agentic_runner.registration import RunnerState
from agentic_runner.sealed_box import (
    RecipientKeyPair,
    RecipientKeyStore,
    SealedCredentialStream,
    open_sealed,
)
from agentic_runner_contracts import __version__ as contracts_version
from agentic_runner_contracts.runner_registration import (
    FloorState,
    HeartbeatAck,
    HeartbeatEnvelope,
)
from agentic_runner_contracts.secret import (
    HolderKind,
    HolderReseal,
    SecretRelease,
    holder_share_binding,
)


def _fixture(name: str) -> dict[str, Any]:
    raw = files("agentic_runner_contracts").joinpath(f"fixtures/{name}.fixture.json")
    return dict(json.loads(raw.read_text(encoding="utf-8")))


SECRET = _fixture("secret")
USER_KEY = _fixture("user-key")


def _reseal(**overrides: str) -> HolderReseal:
    return HolderReseal(
        **{
            "organisation_id": SECRET["organisation_id"],
            "secret_id": SECRET["secret_id"],
            "holder_fingerprint": SECRET["holder_fingerprint"],
            "holder_share": SECRET["holder_share"],
            "user_key_public_key": USER_KEY["user_key_public"],
            **overrides,
        }
    )


def _recipient_keys(tmp_path: Path) -> RecipientKeyStore:
    keys = RecipientKeyStore(tmp_path)
    pair = RecipientKeyPair(
        key_id=SECRET["recipient_key_id"],
        public_key=SECRET["recipient_public_key"],
        private_key=SECRET["recipient_private_key"],
    )
    keys.install(pair, managed_by="test", now=keys.rotated_at())
    return keys


def _organisation_runner(tmp_path: Path) -> LlmKeyOpener:
    return LlmKeyOpener(
        pool_key=parse_pool_key(SECRET["organisation_key_private"]),
        recipient_keys=_recipient_keys(tmp_path / "organisation"),
    )


def test_the_organisation_runner_reseals_a_share_the_members_runner_opens(
    tmp_path: Path,
) -> None:
    [resealed] = _organisation_runner(tmp_path).reseal([_reseal()])

    assert resealed.holder_fingerprint == USER_KEY["user_key_fingerprint"]
    binding = holder_share_binding(
        organisation_id=SECRET["organisation_id"],
        secret_id=SECRET["secret_id"],
        holder_kind=HolderKind.USER_KEY,
        holder_fingerprint=USER_KEY["user_key_fingerprint"],
    )
    assert (
        open_sealed(
            private_key=USER_KEY["user_key_private"],
            binding=binding,
            ciphertext=resealed.holder_share,
        )
        == SECRET["holder_share_plaintext"]
    )
    # The member's Runner holds only their User Key, and opens the value with no retyping.
    members_runner = LlmKeyOpener(
        pool_key=None,
        recipient_keys=_recipient_keys(tmp_path / "member"),
        user_key=parse_pool_key(USER_KEY["user_key_private"]),
    )
    release = SecretRelease.model_validate(
        {
            "organisation_id": SECRET["organisation_id"],
            "recipient_key_id": SECRET["recipient_key_id"],
            "secrets": [
                {
                    "secret_id": SECRET["secret_id"],
                    "name": SECRET["name"],
                    "scope": SECRET["scope"],
                    "holder_kind": "user_key",
                    "holder_fingerprint": resealed.holder_fingerprint,
                    "holder_share": resealed.holder_share,
                    "platform_share": SECRET["platform_share"],
                }
            ],
        }
    )
    assert members_runner.open_secrets(release, names=[SECRET["name"]]) == {
        SECRET["name"]: SECRET["value"]
    }


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"holder_fingerprint": ":".join(["0000"] * 8)}, id="another-organisation-key"),
        pytest.param({"secret_id": str(uuid4())}, id="bound-to-another-secret"),
        pytest.param({"organisation_id": str(uuid4())}, id="bound-to-another-organisation"),
    ],
)
def test_a_share_that_does_not_open_is_skipped_and_the_rest_go_on(
    tmp_path: Path, overrides: dict[str, str]
) -> None:
    resealed = _organisation_runner(tmp_path).reseal([_reseal(**overrides), _reseal()])

    assert [item.secret_id for item in resealed] == [SECRET["secret_id"]]


def test_a_runner_without_the_organisation_key_reseals_nothing(tmp_path: Path) -> None:
    members_runner = LlmKeyOpener(
        pool_key=None,
        recipient_keys=_recipient_keys(tmp_path),
        user_key=parse_pool_key(USER_KEY["user_key_private"]),
    )

    assert members_runner.reseal([_reseal()]) == []


class _Registration:
    server_date = None

    def __init__(self) -> None:
        self.sent: list[HeartbeatEnvelope] = []
        self.reseals: list[HolderReseal] = []

    async def heartbeat(self, state: Any, envelope: HeartbeatEnvelope) -> HeartbeatAck:
        self.sent.append(envelope)
        reseals, self.reseals = self.reseals, []
        return HeartbeatAck(
            runner_id=uuid4(),
            floor_state=FloorState.OK,
            contracts_floor=contracts_version,
            tag_set_version=1,
            accepts_new_directives=True,
            holder_reseals=reseals,
        )


@pytest.mark.asyncio
async def test_the_acks_reseals_are_answered_once_on_the_next_envelope(tmp_path: Path) -> None:
    registration = _Registration()
    runner_id = uuid4()
    stream = service.ControlPlaneStream(
        client=registration,  # type: ignore[arg-type]
        state=RunnerState(
            runner_id=runner_id,
            identity_id="id",
            private_key_pem="unused",
            temporal_namespace="org-x",
            task_queue=f"runner.{runner_id}",
        ),
        isolation=service.IsolationMode.NONE,
        proxy=LlmProxy(slots=SlotStore()),
        sealed=SealedCredentialStream(RecipientKeyStore(tmp_path / "stream")),
        credentials=CredentialResolver(store=EmptyCredentialStore()),
        load=service._LoadInterceptor(),
        llm_keys=_organisation_runner(tmp_path),
    )

    registration.reseals = [_reseal()]
    await stream.exchange([])
    await stream.exchange([])
    await stream.exchange([])

    first, second, third = registration.sent
    assert first.holder_reseals == []
    assert [item.holder_fingerprint for item in second.holder_reseals] == [
        USER_KEY["user_key_fingerprint"]
    ]
    assert third.holder_reseals == []
    # Nothing to report is no key at all: a control plane below 4.4 forbids it.
    assert "holder_reseals" not in third.model_dump(mode="json")
    assert "holder_reseals" in second.model_dump(mode="json")


def test_an_ack_with_no_reseals_omits_the_field() -> None:
    ack = HeartbeatAck(
        runner_id=uuid4(),
        floor_state=FloorState.OK,
        contracts_floor=contracts_version,
        tag_set_version=1,
        accepts_new_directives=True,
    )

    assert "holder_reseals" not in ack.model_dump(mode="json")
