"""The Channel Message envelope and what crosses the control plane about it (PRD issue 52).

Map ticket 10, "The message" and "Where the bytes live": a Message is a structured
envelope an Agent posts on a Channel, input to a sibling's next Directive and never a
Directive itself. **Bodies live in the Runner** (``agentic_runner.message_store``); what
leaves the Runner is one of three things, each its own model here so the partition is a
type, not a convention:

* :class:`MessageMetadata` -- the Evidence Event per Message: sender, Channel, Work
  Record, kind, references, size, time. It has **no body field**;
  ``tests/unit/test_channel_message_store.py`` asserts that structurally.
* :class:`PendingMessagesSignal` -- the wake-up the workflow receives, ids and a count.
* :class:`TranscriptDelivery` -- the one crossing the owner explicitly accepted: the
  liable user asked for a transcript, the Runner hands the bodies back over its signed
  stream, the console shows them and stores nothing.

Sandbox-safe on purpose: ``workflows/ralph.py`` imports the signal name and its payload
dataclass, and that module is re-imported inside Temporal's determinism sandbox.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from agentic_runner_contracts.public_metadata import signal_name

__all__ = [
    "MESSAGE_BODY_MAX_BYTES",
    "VERDICT_BLOCK",
    "VERDICT_CLEAR",
    "PENDING_MESSAGES_SIGNAL",
    "TRANSCRIPT_DELIVERIES_PATH",
    "MessageEnvelope",
    "MessageKind",
    "MessageMetadata",
    "MessageReference",
    "PendingMessagesSignal",
    "ReferenceKind",
    "TranscriptDelivery",
    "TranscriptRequest",
]

# Public Metadata (ADR-0010 §6): Temporal displays a signal name, so it is built by the
# builder and says what the platform does -- "messages are pending" -- never whose.
PENDING_MESSAGES_SIGNAL: Final = signal_name("channel.messages_pending")

# The Runner's transcript delivery rides the same signed stream as the heartbeat.
TRANSCRIPT_DELIVERIES_PATH: Final = "/api/runner/v1/runners/transcripts"

MESSAGE_BODY_MAX_BYTES: Final = 65_536

# The Critic's veto is one bit (PRD issue 54): a `verdict` blocks or clears the head it
# names, and nothing else -- no score, no vote.
VERDICT_BLOCK: Final = "block"
VERDICT_CLEAR: Final = "clear"


class MessageKind(StrEnum):
    """Closed: 10 dropped `proposal` / `vote`, not parked -- there is no voting."""

    NOTE = "note"
    REQUEST = "request"
    RESULT = "result"
    HANDOFF = "handoff"
    VERDICT = "verdict"


class ReferenceKind(StrEnum):
    """What a Message may point at, by platform id -- never by pasting the thing."""

    BRANCH = "branch"
    PULL_REQUEST = "pull_request"
    EVIDENCE_EVENT = "evidence_event"
    WORKSPACE_PATH = "workspace_path"
    MESSAGE = "message"


class MessageReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: ReferenceKind
    value: Annotated[str, StringConstraints(min_length=1, max_length=512)]


class MessageMetadata(BaseModel):
    """The Conversation's shape, as the control plane may hold it (10: never the body)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    message_id: UUID
    sender_agent_id: UUID
    channel_id: UUID
    work_record_id: UUID
    kind: MessageKind
    references: list[MessageReference] = Field(default_factory=list, max_length=32)
    size_bytes: int = Field(ge=0)
    sent_at: datetime
    # Who the Message is for (PRD issue 53's wake rules): an Agent by id, a Role by name
    # (a role-addressed `request` wakes whichever Agent holds it), or neither -- a `note`
    # to the whole Swarm, or a `verdict`, which always goes to the Lead. A `result` names
    # its requester here, resolved by the Runner from the request it answers.
    recipient_agent_id: UUID | None = None
    recipient_role: str | None = Field(default=None, max_length=32)
    # A `verdict`'s one bit and the head commit it reviewed (PRD issue 54). Metadata, not
    # body: the loop's veto is a workflow-state test on exactly these two, and a verdict
    # missing either is kept as a `note` that changes nothing.
    verdict: Literal["block", "clear"] | None = None
    head_sha: str | None = Field(default=None, max_length=64)


class MessageEnvelope(MessageMetadata):
    """The Message itself: the metadata plus the markdown body. Runner-local."""

    body: Annotated[str, StringConstraints(max_length=MESSAGE_BODY_MAX_BYTES)]

    def metadata(self) -> MessageMetadata:
        return MessageMetadata.model_validate(self.model_dump(exclude={"body"}))


@dataclass(frozen=True)
class PendingMessagesSignal:
    """``Agent X has N Messages on Channel C``: ids and a count, nothing else (10).

    ``agent_id`` is the sender and ``count`` the Channel's depth in this Conversation
    after the send. Who is woken by it is the Channel's membership, which the scheduler
    (PRD issue 53) resolves from Grants at the next Directive boundary.
    """

    work_record_id: str
    agent_id: str
    channel_id: str
    count: int
    # PRD issue 53: what the scheduler's wake rules key on -- the kind and the addressee
    # of the one Message this signal announces. Defaulted so a signal from a Runner that
    # predates them still decodes; such a Message reads as a `note` and wakes nobody.
    kind: str = "note"
    message_id: str = ""
    recipient_agent_id: str = ""
    recipient_role: str = ""
    # PRD issue 54: a `verdict`'s bit and the head it names, what the veto keys on.
    verdict: str = ""
    head_sha: str = ""


class TranscriptRequest(BaseModel):
    """One pull-through the control plane relays to the Runner over the heartbeat ack."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: UUID
    work_record_id: UUID
    contract_id: UUID


class TranscriptDelivery(BaseModel):
    """The Runner's answer, signed like a heartbeat: the bodies, or why not.

    ``refused`` carries the Contract state that seals the store (``terminated``) so the
    console can say so; ``messages`` is then empty.
    """

    model_config = ConfigDict(extra="forbid")

    request_id: UUID
    work_record_id: UUID
    refused: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,31}$")] | None = None
    messages: list[MessageEnvelope] = Field(default_factory=list, max_length=10_000)
