"""An Organisation Runner reseals "Also members' Runners" Secrets to members' User Keys
(QTS-2479, Spec: Agent Guardrails rev 7, A1, S5), and only to one an Admin approved
(QTS-2699, rev 10, S5).

What this pins, on ``fixtures/secret.fixture.json`` (an Organisation Secret sealed to the
Organisation Key), ``fixtures/user-key.fixture.json`` (a member's User Key) and
``fixtures/user-key-approval.fixture.json`` (an Admin's approval of that key):

* the Organisation Runner opens the holder share with the Organisation Key and seals it to
  the member's User Key, bound to the fingerprint it computes itself; the member's Runner
  then opens the Secret with no retyping;
* a share sealed to another Organisation Key, or bound to another Secret, is not resealed,
  and a Runner without the Organisation Key reseals nothing;
* a reseal with no approval tag, or a tag for another member, Organisation or fingerprint,
  or one keyed by anything but the Organisation Key, is refused and its key reported
  unapproved, once; one naming no member (a 4.4 control plane) is refused unreported;
* a reseal with no availability tag (QTS-2701, rev 12, S5), or one for another Secret or
  Organisation, keyed by anything but the Organisation Key, or an approval tag in its
  place, is refused unreported even though the member is approved
  (``fixtures/secret-availability.fixture.json``);
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
from agentic_runner.pool_key import LlmKeyOpener, ResealOutcome, parse_pool_key
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
from agentic_runner_contracts.sealed_credential import key_fingerprint
from agentic_runner_contracts.secret import (
    HolderKind,
    HolderReseal,
    SecretRelease,
    UnapprovedUserKey,
    holder_share_binding,
)
from agentic_runner_contracts.secret_availability import availability_tag
from agentic_runner_contracts.user_key_approval import approval_tag


def _fixture(name: str) -> dict[str, Any]:
    raw = files("agentic_runner_contracts").joinpath(f"fixtures/{name}.fixture.json")
    return dict(json.loads(raw.read_text(encoding="utf-8")))


SECRET = _fixture("secret")
USER_KEY = _fixture("user-key")
APPROVAL = _fixture("user-key-approval")
AVAILABILITY = _fixture("secret-availability")
UNAPPROVED = UnapprovedUserKey(
    organisation_id=SECRET["organisation_id"],
    user_id=USER_KEY["user_profile_id"],
    user_key_fingerprint=USER_KEY["user_key_fingerprint"],
)


def _reseal(**overrides: str | None) -> HolderReseal:
    return HolderReseal(
        **{
            "organisation_id": SECRET["organisation_id"],
            "secret_id": SECRET["secret_id"],
            "holder_fingerprint": SECRET["holder_fingerprint"],
            "holder_share": SECRET["holder_share"],
            "user_key_public_key": USER_KEY["user_key_public"],
            "user_id": USER_KEY["user_profile_id"],
            "approval_tag": APPROVAL["approval_tag"],
            "availability_tag": AVAILABILITY["availability_tag"],
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
    outcome = _organisation_runner(tmp_path).reseal([_reseal()])

    [resealed] = outcome.resealed
    assert outcome.unapproved == []

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
        # Marked there, so the share's own binding is what refuses it.
        pytest.param(
            {
                "secret_id": (other_secret := str(uuid4())),
                "availability_tag": availability_tag(
                    organisation_key_private=SECRET["organisation_key_private"],
                    organisation_id=SECRET["organisation_id"],
                    secret_id=other_secret,
                ),
            },
            id="bound-to-another-secret",
        ),
        # Approved and marked there, so the share's own binding is what refuses it.
        pytest.param(
            {
                "organisation_id": (other := str(uuid4())),
                "approval_tag": approval_tag(
                    organisation_key_private=SECRET["organisation_key_private"],
                    organisation_id=other,
                    user_id=USER_KEY["user_profile_id"],
                    user_key_fingerprint=USER_KEY["user_key_fingerprint"],
                ),
                "availability_tag": availability_tag(
                    organisation_key_private=SECRET["organisation_key_private"],
                    organisation_id=other,
                    secret_id=SECRET["secret_id"],
                ),
            },
            id="bound-to-another-organisation",
        ),
    ],
)
def test_a_share_that_does_not_open_is_skipped_and_the_rest_go_on(
    tmp_path: Path, overrides: dict[str, str]
) -> None:
    outcome = _organisation_runner(tmp_path).reseal([_reseal(**overrides), _reseal()])

    assert [item.secret_id for item in outcome.resealed] == [SECRET["secret_id"]]
    assert outcome.unapproved == []


@pytest.mark.parametrize(
    "tag",
    [
        pytest.param(None, id="no-tag"),
        *(
            pytest.param(refused["approval_tag"], id=refused["why"].replace(" ", "-"))
            for refused in APPROVAL["refused"]
        ),
    ],
)
def test_a_user_key_with_no_valid_approval_is_refused_and_reported(
    tmp_path: Path, tag: str | None
) -> None:
    outcome = _organisation_runner(tmp_path).reseal(
        [_reseal(approval_tag=tag), _reseal(approval_tag=tag)]
    )

    assert outcome.resealed == []
    # One report per key, not per Secret, naming the tag refused.
    assert outcome.unapproved == [UNAPPROVED.model_copy(update={"approval_tag": tag})]


@pytest.mark.parametrize(
    "tag",
    [
        pytest.param(None, id="no-tag"),
        *(
            pytest.param(refused["availability_tag"], id=refused["why"].replace(" ", "-"))
            for refused in AVAILABILITY["refused"]
        ),
    ],
)
def test_a_secret_with_no_valid_availability_tag_is_refused_and_not_reported(
    tmp_path: Path, tag: str | None
) -> None:
    # The member is approved; the Secret is what an Admin never marked here.
    outcome = _organisation_runner(tmp_path).reseal([_reseal(availability_tag=tag)])

    assert outcome == ResealOutcome()


def test_a_valid_availability_tag_does_not_carry_to_another_secret(tmp_path: Path) -> None:
    # The control plane keeps a marked Secret's tag and hands it with another Secret's
    # share: the tag is checked on the Secret the share is bound to.
    outcome = _organisation_runner(tmp_path).reseal([_reseal(secret_id=str(uuid4()))])

    assert outcome == ResealOutcome()


def test_a_valid_tag_does_not_approve_another_members_key(tmp_path: Path) -> None:
    # The control plane swaps in a key of its own and keeps the member's tag: the tag is
    # over the fingerprint, which the Runner computes from the key it would seal to.
    outcome = _organisation_runner(tmp_path).reseal(
        [_reseal(user_key_public_key=SECRET["recipient_public_key"])]
    )

    assert outcome.resealed == []
    assert [item.user_key_fingerprint for item in outcome.unapproved] == [
        key_fingerprint(SECRET["recipient_public_key"])
    ]


def test_a_reseal_naming_no_member_is_refused_and_not_reported(tmp_path: Path) -> None:
    outcome = _organisation_runner(tmp_path).reseal([_reseal(user_id=None, approval_tag=None)])

    assert outcome == ResealOutcome()


def test_a_runner_without_the_organisation_key_reseals_nothing(tmp_path: Path) -> None:
    members_runner = LlmKeyOpener(
        pool_key=None,
        recipient_keys=_recipient_keys(tmp_path),
        user_key=parse_pool_key(USER_KEY["user_key_private"]),
    )

    assert members_runner.reseal([_reseal()]) == ResealOutcome()


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
    assert "unapproved_user_keys" not in second.model_dump(mode="json")


@pytest.mark.asyncio
async def test_an_unapproved_key_is_reported_once_on_the_next_envelope(tmp_path: Path) -> None:
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

    registration.reseals = [_reseal(approval_tag=None)]
    await stream.exchange([])
    await stream.exchange([])
    await stream.exchange([])

    _, second, third = registration.sent
    assert second.holder_reseals == []
    assert second.unapproved_user_keys == [UNAPPROVED]  # no tag was sent, none is named
    assert "unapproved_user_keys" not in third.model_dump(mode="json")


def test_an_ack_with_no_reseals_omits_the_field() -> None:
    ack = HeartbeatAck(
        runner_id=uuid4(),
        floor_state=FloorState.OK,
        contracts_floor=contracts_version,
        tag_set_version=1,
        accepts_new_directives=True,
    )

    assert "holder_reseals" not in ack.model_dump(mode="json")
