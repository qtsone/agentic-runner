"""The Message store, the `channel.*` seam and the wake signal (PRD issue 52).

`.scratch/multi-org-release-1/issues/52-message-store-conversation-wakeups-transcript.md`.
Map ticket 10 held decision 10 strictly: bodies live in the Runner, and what crosses is
metadata (Evidence), ids and a count (the signal), or a transcript the liable user asked
for. Each of those is asserted here against the real store, the real seam and a real
attempt socket. That no control-plane module imports the store is agentic-os's own
partition gate, not this repository's.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from agentic_runner.activities import RunnerRalphActivities, _RuntimeContextState
from agentic_runner.callback import (
    AttemptCallbackServer,
    CallbackError,
    MessageListRequest,
    MessageSendRequest,
    call,
)
from agentic_runner.message_store import MessageStore
from agentic_runner_contracts.activity_io import ContractResidueInput
from agentic_runner_contracts.channel_messages import (
    PENDING_MESSAGES_SIGNAL,
    MessageEnvelope,
    MessageKind,
    MessageMetadata,
    PendingMessagesSignal,
    TranscriptRequest,
)
from agentic_runner_contracts.grants import GrantSnapshot
from agentic_runner_contracts.public_metadata import signal_name

WORK_RECORD_ID = "123e4567-e89b-12d3-a456-426614174000"
AGENT_ID = "8f14e45f-ceea-467a-9a37-1a2b3c4d5e6f"
CONTRACT_ID = "1d0f6b8c-2e3f-4a5b-8c7d-9e0f1a2b3c4d"
CHANNEL_ID = "5b7f0c2e-9a1d-4e3b-8c6f-2d4a6b8c0e1f"
CHANNEL_SELECTOR = "platform/backend"
EVALUATION_SOURCE = "ralph.grant_evaluation"
MESSAGE_SOURCE = "channel.message"


class _FakeClient:
    def __init__(self) -> None:
        self.evidence: list[tuple[str, dict[str, Any]]] = []
        self.expired: list[str] = []

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

    async def list_expired_workspaces(self, work_record_ids: list[str]) -> list[str]:
        return [item for item in work_record_ids if item in self.expired]

    def of(self, source: str) -> list[dict[str, Any]]:
        return [payload for name, payload in self.evidence if name == source]


@dataclass
class _FakeSignaller:
    sent: list[PendingMessagesSignal]

    async def signal(self, pending: PendingMessagesSignal) -> None:
        self.sent.append(pending)


def _grant(read: str, write: str) -> dict[str, Any]:
    return {
        "entries": [
            {
                "resource_type": "channel",
                "selector": CHANNEL_SELECTOR,
                "verbs": {"read": read, "write": write},
            }
        ]
    }


def _state(*, read: str = "allow", write: str = "allow") -> _RuntimeContextState:
    snapshot = GrantSnapshot.from_payload(
        {
            "agent_id": AGENT_ID,
            "contract_id": CONTRACT_ID,
            "contract_state": "active",
            "dispatchable": True,
            "root_grant": _grant(read, write),
            "agent_grant": _grant(read, write),
            "persona_allow_list": _grant(read, write),
            "resources": [
                {
                    "resource_type": "channel",
                    "selector": CHANNEL_SELECTOR,
                    "in_contract_scope": True,
                    "resource_id": CHANNEL_ID,
                }
            ],
        }
    )
    return _RuntimeContextState(
        reviewer=None,
        repository="qts/agentic-os",
        verifier_argv=("true",),
        work_branch="agent/work",
        completion_criteria="ship it",
        agent_id=AGENT_ID,
        contract_id=CONTRACT_ID,
        grant_snapshot=snapshot,
    )


@pytest.fixture
def socket_dir() -> Iterator[Path]:
    path = Path(tempfile.mkdtemp(dir="/tmp"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _activities(
    client: _FakeClient, store: MessageStore, signaller: _FakeSignaller | None = None
) -> RunnerRalphActivities:
    return RunnerRalphActivities(
        client,  # type: ignore[arg-type]
        message_store=store,
        workflow_signaller=signaller,
    )


def _send(body: str = "the branch is green", **fields: Any) -> dict[str, Any]:
    return {"channel_id": CHANNEL_ID, "kind": "note", "body": body, **fields}


async def _raw_status(socket_path: Path, path: str) -> int:
    """A request with no bearer at all -- what a stray subprocess on the box would send."""

    reader, writer = await asyncio.open_unix_connection(str(socket_path))
    body = b'{"channel_id": "x", "body": "y"}'
    writer.write(
        f"POST {path} HTTP/1.1\r\nHost: localhost\r\nContent-Length: {len(body)}\r\n\r\n".encode()
        + body
    )
    await writer.drain()
    status_line = await reader.readline()
    writer.close()
    return int(status_line.split()[1])


async def _post(
    server: AttemptCallbackServer, path: str, payload: dict[str, Any], token: str
) -> dict[str, Any]:
    return await asyncio.to_thread(
        call, socket_path=server.socket_path, token=token, path=path, payload=payload
    )


# ------------------------------------------------------------------- the seam (AC1)


@pytest.mark.asyncio
async def test_send_under_channel_write_deny_refuses_with_evidence_and_stores_nothing(
    tmp_path: Path,
) -> None:
    client = _FakeClient()
    store = MessageStore(tmp_path / "messages")
    signaller = _FakeSignaller(sent=[])
    handlers = _activities(client, store, signaller)._callback_handlers(
        WORK_RECORD_ID, _state(write="deny"), tmp_path
    )

    answer = await handlers.message_send(MessageSendRequest(**_send()))

    assert answer.accepted is False
    assert answer.decision == "deny"
    [evaluation] = client.of(EVALUATION_SOURCE)
    assert evaluation["agent_id"] == AGENT_ID
    assert evaluation["verb"] == "write"
    assert evaluation["resource_type"] == "channel"
    assert evaluation["resource"] == CHANNEL_SELECTOR
    assert evaluation["deciding_entry"] == {
        "resource_type": "channel",
        "selector": CHANNEL_SELECTOR,
        "decision": "deny",
    }
    assert client.of(MESSAGE_SOURCE) == []
    assert store.held() == []
    assert signaller.sent == []


@pytest.mark.asyncio
async def test_send_under_allow_stores_the_envelope_records_metadata_and_wakes_the_loop(
    tmp_path: Path,
) -> None:
    client = _FakeClient()
    store = MessageStore(tmp_path / "messages")
    signaller = _FakeSignaller(sent=[])
    handlers = _activities(client, store, signaller)._callback_handlers(
        WORK_RECORD_ID, _state(), tmp_path
    )
    first = await handlers.message_send(
        MessageSendRequest(**_send(references=[{"kind": "pull_request", "value": "42"}]))
    )
    second = await handlers.message_send(MessageSendRequest(**_send("and the tests pass")))

    assert first.accepted and second.accepted
    stored = store.read(contract_id=CONTRACT_ID, work_record_id=WORK_RECORD_ID)
    assert [envelope.body for envelope in stored] == ["the branch is green", "and the tests pass"]
    assert stored[0].references[0].value == "42"
    assert str(stored[0].message_id) == first.message_id
    # The evaluation, allowed, then the metadata -- and no body anywhere in the trail.
    assert [name for name, _ in client.evidence] == [
        EVALUATION_SOURCE,
        MESSAGE_SOURCE,
        EVALUATION_SOURCE,
        MESSAGE_SOURCE,
    ]
    metadata = client.of(MESSAGE_SOURCE)[0]
    assert metadata["sender_agent_id"] == AGENT_ID
    assert metadata["channel_id"] == CHANNEL_ID
    assert metadata["work_record_id"] == WORK_RECORD_ID
    assert metadata["kind"] == "note"
    assert metadata["size_bytes"] == len("the branch is green")
    assert metadata["references"] == [{"kind": "pull_request", "value": "42"}]
    assert "body" not in metadata
    assert "the branch is green" not in repr(client.evidence)
    # The wake: ids and the Channel's depth, once per send -- plus, since PRD issue 53,
    # the kind and the addressee the scheduler's wake rules key on (none here: a note).
    assert signaller.sent == [
        PendingMessagesSignal(
            work_record_id=WORK_RECORD_ID,
            agent_id=AGENT_ID,
            channel_id=CHANNEL_ID,
            count=1,
            kind="note",
            message_id=first.message_id,
        ),
        PendingMessagesSignal(
            work_record_id=WORK_RECORD_ID,
            agent_id=AGENT_ID,
            channel_id=CHANNEL_ID,
            count=2,
            kind="note",
            message_id=second.message_id,
        ),
    ]
    assert signal_name("channel.messages_pending") == PENDING_MESSAGES_SIGNAL


@pytest.mark.asyncio
async def test_an_addressed_send_wakes_with_its_kind_and_addressee(tmp_path: Path) -> None:
    """PRD issue 53: the signal carries what the scheduler's wake rules key on, and a
    `result` that names nobody answers the `message` it references -- its requester."""

    client = _FakeClient()
    store = MessageStore(tmp_path / "messages")
    signaller = _FakeSignaller(sent=[])
    builder = str(uuid4())
    handlers = _activities(client, store, signaller)._callback_handlers(
        WORK_RECORD_ID, _state(), tmp_path
    )

    request = await handlers.message_send(
        MessageSendRequest(**_send("please add the test", kind="request", recipient_role="builder"))
    )
    handoff = await handlers.message_send(
        MessageSendRequest(
            **_send(
                "you lead now", kind="handoff", recipient_agent_id=builder, recipient_role="lead"
            )
        )
    )
    # A result posted *by the builder* answering the request: the store resolves the
    # requester from the referenced Message. Sender is whoever holds the attempt; here the
    # same state, so the requester resolves to AGENT_ID.
    answer = await handlers.message_send(
        MessageSendRequest(
            **_send(
                "done", kind="result", references=[{"kind": "message", "value": request.message_id}]
            )
        )
    )

    assert [
        (s.kind, s.recipient_agent_id, s.recipient_role, s.message_id) for s in signaller.sent
    ] == [
        ("request", "", "builder", request.message_id),
        ("handoff", builder, "lead", handoff.message_id),
        ("result", AGENT_ID, "", answer.message_id),
    ]
    metadata = client.of(MESSAGE_SOURCE)
    assert metadata[0]["recipient_role"] == "builder"
    assert metadata[1]["recipient_agent_id"] == builder
    assert "body" not in metadata[2]


def test_pending_is_what_one_member_has_not_seen_addressed_to_it(tmp_path: Path) -> None:
    store = MessageStore(tmp_path / "messages")
    lead, builder, critic = str(uuid4()), str(uuid4()), str(uuid4())

    def send(sender: str, kind: MessageKind, body: str, **to: str | None) -> MessageEnvelope:
        return store.append(
            contract_id=CONTRACT_ID,
            work_record_id=WORK_RECORD_ID,
            sender_agent_id=sender,
            channel_id=CHANNEL_ID,
            kind=kind,
            body=body,
            references=[],
            **to,
        )

    send(lead, MessageKind.REQUEST, "to the builder by id", recipient_agent_id=builder)
    send(lead, MessageKind.REQUEST, "to whoever builds", recipient_role="builder")
    send(lead, MessageKind.NOTE, "to everyone")
    send(lead, MessageKind.REQUEST, "to the critic", recipient_agent_id=critic)
    send(builder, MessageKind.NOTE, "my own note")

    def pending(agent_id: str, *roles: str) -> list[str]:
        return [
            envelope.body
            for envelope in store.pending(
                contract_id=CONTRACT_ID,
                work_record_id=WORK_RECORD_ID,
                channel_id=CHANNEL_ID,
                agent_id=agent_id,
                roles=roles,
            )
        ]

    # By id, by a held role, and the Swarm-wide note; never its own, never the critic's.
    assert pending(builder, "builder") == [
        "to the builder by id",
        "to whoever builds",
        "to everyone",
    ]
    assert pending(critic, "critic") == ["to everyone", "to the critic", "my own note"]

    # Consumed through the last one folded in: the next Directive sees only what came after.
    seen = store.pending(
        contract_id=CONTRACT_ID,
        work_record_id=WORK_RECORD_ID,
        channel_id=CHANNEL_ID,
        agent_id=builder,
        roles=("builder",),
    )
    store.mark_consumed(
        contract_id=CONTRACT_ID, work_record_id=WORK_RECORD_ID, agent_id=builder, through=seen[-1]
    )
    assert pending(builder, "builder") == []
    send(critic, MessageKind.RESULT, "reviewed", recipient_agent_id=builder)
    assert pending(builder, "builder") == ["reviewed"]
    # The cursor goes with the store on the retention drop.
    assert store.remove(CONTRACT_ID, WORK_RECORD_ID) is True
    assert not list((tmp_path / "messages").rglob("*.cursors.json"))


@pytest.mark.asyncio
async def test_list_under_channel_read_deny_returns_nothing_with_evidence(tmp_path: Path) -> None:
    client = _FakeClient()
    store = MessageStore(tmp_path / "messages")
    store.append(
        contract_id=CONTRACT_ID,
        work_record_id=WORK_RECORD_ID,
        sender_agent_id=AGENT_ID,
        channel_id=CHANNEL_ID,
        kind=MessageKind.NOTE,
        body="already there",
        references=[],
    )
    handlers = _activities(client, store)._callback_handlers(
        WORK_RECORD_ID, _state(read="deny"), tmp_path
    )

    answer = await handlers.message_list(MessageListRequest(channel_id=CHANNEL_ID))

    assert answer.allowed is False
    assert answer.messages == []
    [evaluation] = client.of(EVALUATION_SOURCE)
    assert (evaluation["verb"], evaluation["decision"]) == ("read", "deny")


@pytest.mark.asyncio
async def test_a_channel_the_registry_does_not_carry_is_out_of_scope(tmp_path: Path) -> None:
    client = _FakeClient()
    handlers = _activities(client, MessageStore(tmp_path / "messages"))._callback_handlers(
        WORK_RECORD_ID, _state(), tmp_path
    )
    unknown = str(uuid4())

    answer = await handlers.message_send(MessageSendRequest(**_send(channel_id=unknown)))

    assert answer.accepted is False
    [evaluation] = client.of(EVALUATION_SOURCE)
    assert evaluation["resource"] == unknown
    assert evaluation["deciding_link"] is None


# --------------------------------------------------------------- the socket (AC2)


@pytest.mark.asyncio
async def test_without_the_attempts_token_the_store_is_unreachable(
    tmp_path: Path, socket_dir: Path
) -> None:
    client = _FakeClient()
    store = MessageStore(tmp_path / "messages")
    server = AttemptCallbackServer(
        socket_path=socket_dir / "s.sock",
        handlers=_activities(client, store)._callback_handlers(WORK_RECORD_ID, _state(), tmp_path),
    )
    async with server:
        no_token = await _raw_status(server.socket_path, "/v0/message/send")
        with pytest.raises(CallbackError) as wrong_token:
            await _post(server, "/v0/message/send", _send(), "another-attempts-bearer")
        with pytest.raises(CallbackError) as wrong_list:
            await _post(server, "/v0/message/list", {"channel_id": CHANNEL_ID}, "x")
        accepted = await _post(server, "/v0/message/send", _send(), server.token)
        listed = await _post(server, "/v0/message/list", {"channel_id": CHANNEL_ID}, server.token)

    assert no_token == 401
    assert wrong_token.value.status_code == 401
    assert wrong_list.value.status_code == 401
    assert accepted["accepted"] is True
    assert [message["body"] for message in listed["messages"]] == ["the branch is green"]
    # The file itself is the Runner's: closed to every other uid, the Contract's included.
    [(contract_id, work_record_id)] = store.held()
    path = store.root / contract_id / f"{work_record_id}.jsonl"
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700


# --------------------------------------------------------- seal and retention (AC4)


@pytest.mark.asyncio
async def test_a_terminated_contract_seals_its_stores_and_retention_drops_them(
    tmp_path: Path,
) -> None:
    client = _FakeClient()
    store = MessageStore(tmp_path / "messages")
    store.append(
        contract_id=CONTRACT_ID,
        work_record_id=WORK_RECORD_ID,
        sender_agent_id=AGENT_ID,
        channel_id=CHANNEL_ID,
        kind=MessageKind.RESULT,
        body="kept for the org",
        references=[],
    )
    activities = _activities(client, store)

    residue = await activities.wipe_contract_residue(ContractResidueInput(contract_id=CONTRACT_ID))

    assert residue.message_stores_sealed == 1
    assert store.sealed_state(CONTRACT_ID) == "terminated"
    assert store.held() == [(CONTRACT_ID, WORK_RECORD_ID)]
    with pytest.raises(PermissionError):
        store.read(contract_id=CONTRACT_ID, work_record_id=WORK_RECORD_ID)
    # The seam refuses too, even with a Grant that would allow it.
    handlers = activities._callback_handlers(WORK_RECORD_ID, _state(), tmp_path)
    refused = await handlers.message_list(MessageListRequest(channel_id=CHANNEL_ID))
    assert refused.allowed is False and "sealed" in refused.reason
    # The pull-through is answered with the state, not the bodies.
    delivery = store.transcript(
        TranscriptRequest(
            request_id=uuid4(), work_record_id=UUID(WORK_RECORD_ID), contract_id=UUID(CONTRACT_ID)
        )
    )
    assert delivery.refused == "terminated" and delivery.messages == []

    # Kept until retention: not expired, not dropped; expired, dropped -- seal and all.
    swept = await activities.delete_expired_workspaces()
    assert (swept.message_stores_deleted, store.held()) == (0, [(CONTRACT_ID, WORK_RECORD_ID)])
    client.expired = [WORK_RECORD_ID]
    swept = await activities.delete_expired_workspaces()
    assert swept.message_stores_deleted == 1
    assert store.held() == []
    assert not (store.root / CONTRACT_ID).exists()
    assert {"contract_id": CONTRACT_ID, "message_stores_deleted": 1} in client.of(
        "ralph.workspace_retention"
    )


def test_a_transcript_carries_every_body_of_the_conversation(tmp_path: Path) -> None:
    store = MessageStore(tmp_path / "messages")
    for body in ("one", "two"):
        store.append(
            contract_id=CONTRACT_ID,
            work_record_id=WORK_RECORD_ID,
            sender_agent_id=AGENT_ID,
            channel_id=CHANNEL_ID,
            kind=MessageKind.NOTE,
            body=body,
            references=[],
        )

    delivery = store.transcript(
        TranscriptRequest(
            request_id=uuid4(), work_record_id=UUID(WORK_RECORD_ID), contract_id=UUID(CONTRACT_ID)
        )
    )

    assert delivery.refused is None
    assert [message.body for message in delivery.messages] == ["one", "two"]


# ------------------------------------------------------------------- structural


def test_the_message_evidence_model_has_no_body_field() -> None:
    assert "body" not in MessageMetadata.model_fields
    assert "body" in MessageEnvelope.model_fields
    envelope = MessageEnvelope(
        message_id=uuid4(),
        sender_agent_id=UUID(AGENT_ID),
        channel_id=UUID(CHANNEL_ID),
        work_record_id=UUID(WORK_RECORD_ID),
        kind=MessageKind.NOTE,
        size_bytes=3,
        sent_at="2026-09-23T10:00:00+00:00",  # type: ignore[arg-type]
        body="hey",
    )
    assert "hey" not in envelope.metadata().model_dump_json()


@pytest.mark.asyncio
async def test_a_verdict_carries_its_bit_and_head_on_the_metadata_and_the_wake(
    tmp_path: Path,
) -> None:
    # PRD issue 54: the veto is a workflow-state test on exactly these two fields, so
    # they cross as metadata; the findings stay the body, in the Runner.
    client = _FakeClient()
    store = MessageStore(tmp_path / "messages")
    signaller = _FakeSignaller(sent=[])
    handlers = _activities(client, store, signaller)._callback_handlers(
        WORK_RECORD_ID, _state(), tmp_path
    )

    sent = await handlers.message_send(
        MessageSendRequest(
            **_send(
                "the reader still selects the dropped column",
                kind="verdict",
                verdict="block",
                head_sha="c" * 40,
            )
        )
    )

    assert sent.accepted
    metadata = client.of(MESSAGE_SOURCE)[0]
    assert (metadata["kind"], metadata["verdict"], metadata["head_sha"]) == (
        "verdict",
        "block",
        "c" * 40,
    )
    assert "dropped column" not in repr(client.evidence)
    [wake] = signaller.sent
    assert (wake.kind, wake.verdict, wake.head_sha) == ("verdict", "block", "c" * 40)
