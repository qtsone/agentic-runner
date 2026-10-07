"""User-connected Sources on the wire between a Runner and the control plane (PRD issue 50).

Map ticket 16 A3: a user-connected Source is the contracted user's own mailbox or Slack
identity, read on the Runner *they* host with a credential that never leaves it. The
control plane holds the Source row -- which Product, the Credential Reference *name*, the
Intake Filter, the reply toggle -- and pushes it to that Runner on the heartbeat ack. What
comes back is only what the Intake Lead decided is work:

* :class:`IntakeWorkRequest` -- the Runner's one inbound "create" (16 A10): a Work Record
  for a Product its Organisation already serves, validated against the Intake Lead
  assignment before anything is written.
* :class:`IntakeIgnoredReport` -- an ``ignore`` leaves one Evidence Event on the Agent,
  ids and classification only. It has **no body field** and forbids extras, so a Runner
  cannot ship the message it ignored even by mistake (16 A6).
"""

from __future__ import annotations

import hashlib
from enum import StrEnum
from typing import Annotated, Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

__all__ = [
    "MEMBER_INSTALLS_DISABLED",
    "MEMBER_INSTALLS_DISABLED_MESSAGE",
    "IntakeIgnoredReport",
    "IntakeWorkRequest",
    "IntakeWorkResponse",
    "SourceIntakeFilter",
    "SourceStatus",
    "TriageCandidateRef",
    "UserSourceAssignment",
    "UserSourceKind",
    "message_ref",
]

# The Slack user connector's one documented refusal (PRD issue 50): a workspace whose
# admins disallow member app installs never issues the user a token worth holding. The
# console prints this message verbatim beside the Source the Runner could not connect.
MEMBER_INSTALLS_DISABLED: Final = "member_installs_disabled"
MEMBER_INSTALLS_DISABLED_MESSAGE: Final = (
    "This Slack workspace does not allow members to install apps, so your Slack identity "
    "cannot be connected. Ask a workspace admin to allow member installs, or connect your "
    "mailbox instead."
)

ErrorCode = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
# A message is referred to by a digest of its own id, never by the id: an RFC 5322
# Message-ID carries the sender's host, and Evidence on the control plane should not.
MessageRef = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{16,64}$")]


def message_ref(message_id: str) -> str:
    """The digest a message is named by outside the Runner."""

    return hashlib.sha256(message_id.encode("utf-8")).hexdigest()[:32]


class UserSourceKind(StrEnum):
    EMAIL = "email"
    SLACK_USER = "slack_user"


class SourceIntakeFilter(BaseModel):
    """The Intake Filter a user-connected Source applies on the Runner, before any token.

    Narrow by default (16 A4): ``addressed_to_me`` from anyone, in ``INBOX`` only.
    ``sender_ids`` empty means every sender; for email it is addresses, for Slack member ids.
    """

    model_config = ConfigDict(extra="forbid")

    folders: list[Annotated[str, StringConstraints(min_length=1, max_length=128)]] = Field(
        default_factory=lambda: ["INBOX"], max_length=16
    )
    sender_ids: list[Annotated[str, StringConstraints(min_length=1, max_length=320)]] = Field(
        default_factory=list, max_length=64
    )
    addressing_rule: Literal[
        "mentions_me", "addressed_to_me", "replies_in_my_threads", "everything"
    ] = "addressed_to_me"


class TriageCandidateRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: UUID
    specialisation: Annotated[str, StringConstraints(max_length=64)]


class UserSourceAssignment(BaseModel):
    """One Source this Runner reads, and everything its Triage Directive needs.

    Pushed on every ack and authoritative, like ``sealed_credentials``: a Source removed
    or re-pointed in the console stops being polled on the next beat. Nothing secret:
    ``credential_reference`` is the *name* the value is stored under on this host.
    """

    model_config = ConfigDict(extra="forbid")

    source_id: UUID
    product_id: UUID
    kind: UserSourceKind
    # The mailbox address, or the Slack member id ("addressed to me" is about this).
    address: Annotated[str, StringConstraints(min_length=1, max_length=320)]
    credential_reference: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    # Email only: where the mailbox is. Plain config, not a secret.
    imap_host: Annotated[str, StringConstraints(max_length=255)] | None = None
    imap_port: int = Field(default=993, ge=1, le=65535)
    smtp_host: Annotated[str, StringConstraints(max_length=255)] | None = None
    smtp_port: int = Field(default=465, ge=1, le=65535)
    intake_filter: SourceIntakeFilter = Field(default_factory=SourceIntakeFilter)
    reply_enabled: bool = False
    intake_lead_agent_id: UUID
    intake_lead_contract_id: UUID
    intake_brief: Annotated[str, StringConstraints(max_length=4000)] | None = None
    candidates: list[TriageCandidateRef] = Field(default_factory=list, max_length=64)


class SourceStatus(BaseModel):
    """Whether the Runner could connect a Source, as a code (heartbeat envelope item)."""

    model_config = ConfigDict(extra="forbid")

    source_id: UUID
    connected: bool
    error_code: ErrorCode | None = None


class IntakeWorkRequest(BaseModel):
    """Create a Work Record for an ``own`` or ``ask`` outcome (16 A10), signed by the Runner.

    ``description`` is the message -- the one place its text leaves the host, and only
    because the Intake Lead decided it is work. ``requester`` is the Source's own
    identifier for the sender (CONTEXT.md, Requester); the control plane binds a User
    Profile when that identifier is one, and never requires a Chat Identity Mapping.
    """

    model_config = ConfigDict(extra="forbid")

    source_id: UUID
    product_id: UUID
    directive_id: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    outcome: Literal["own", "ask"]
    classification_source: Literal["llm", "fallback"]
    reason: Annotated[str, StringConstraints(max_length=500)]
    # The chosen Lead for ``own`` (16 A8); ``ask`` is held on the Intake Lead itself.
    lead_agent_id: UUID | None = None
    description: Annotated[str, StringConstraints(min_length=1, max_length=4000)]
    requester: Annotated[str, StringConstraints(min_length=1, max_length=320)]
    budget_max_tokens: int | None = Field(default=None, ge=0)
    message_refs: list[MessageRef] = Field(min_length=1, max_length=50)


class IntakeWorkResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    work_record_id: UUID
    # ``ask`` with the reply toggle off: the question went to the owner's rail and email.
    notified_owner: bool = False


class IntakeIgnoredReport(BaseModel):
    """An ``ignore`` outcome: ids and classification, never a body (16 A6)."""

    model_config = ConfigDict(extra="forbid")

    source_id: UUID
    product_id: UUID
    directive_id: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    classification_source: Literal["llm", "fallback"]
    message_refs: list[MessageRef] = Field(min_length=1, max_length=50)
