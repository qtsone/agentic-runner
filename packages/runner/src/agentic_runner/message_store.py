"""The per-Work-Record Message store and the wake signal (PRD issue 52, map ticket 10).

Where Channel bodies live. Decision 10 held, stricter than 13: Directive payloads cross
into the control plane unencrypted in Release 1, **Channel bodies never do**. One
Conversation per Work Record, append-only, keyed by Channel; owned by the Runner
*process* and reached by the Agent subprocess only through its attempt socket
(``agentic-runner message send | list``), so a Contract's uid never reads the file
(17 A1). The tree therefore lives under the Runner's own state directory -- never under
the Contract's uid-owned Workspace tree -- with ``0700`` directories and ``0600`` files.

Lifetime: kept for the Organisation's one retention period and dropped by the same
sweep that deletes Workspaces. Contract termination **seals** the Contract's stores: the
org retains, the user loses access, the marker carries the state so a pull-through can
say why it was refused.

Three things leave this module, and this docstring is where the partition is stated:
``MessageMetadata`` as Evidence (no body), :data:`PENDING_MESSAGES_SIGNAL` as a Temporal
signal (ids and a count), and a ``TranscriptDelivery`` over the Runner's signed stream
when the liable user asks. Nothing else reads a body.
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal, Protocol
from uuid import UUID, uuid4

from temporalio.client import Client

from agentic_runner_contracts.channel_messages import (
    PENDING_MESSAGES_SIGNAL,
    MessageEnvelope,
    MessageKind,
    MessageReference,
    PendingMessagesSignal,
    ReferenceKind,
    TranscriptDelivery,
    TranscriptRequest,
)
from agentic_runner_contracts.public_metadata import OUTCOME_KINDS, workflow_id

__all__ = [
    "MESSAGES_DIR",
    "SEALED_MARKER",
    "MessageStore",
    "TemporalWorkflowSignaller",
    "WorkflowSignaller",
]

MESSAGES_DIR: Final = "messages"
SEALED_MARKER: Final = "SEALED"
_DIR_MODE: Final = 0o700
_FILE_MODE: Final = 0o600


class WorkflowSignaller(Protocol):
    async def signal(self, pending: PendingMessagesSignal) -> None:
        """Wake the Work Record's workflow with ids and a count, nothing else."""


class TemporalWorkflowSignaller:
    """The wake signal over the Runner's own namespace connection.

    A Runner polls exactly one namespace -- its Organisation's (map ticket 15 §2) -- and
    the client it polls with is the one this sends on, so the signal cannot land in any
    other Organisation's namespace by construction. The workflow id is built by the
    Public Metadata builder like every Temporal-visible name (ADR-0010 §6).
    """

    def __init__(self, client: Client) -> None:
        self._client = client

    async def signal(self, pending: PendingMessagesSignal) -> None:
        handle = self._client.get_workflow_handle(
            workflow_id(outcome_kind=OUTCOME_KINDS[0], work_record_id=UUID(pending.work_record_id))
        )
        await handle.signal(PENDING_MESSAGES_SIGNAL, pending)


class MessageStore:
    """``<root>/<contract_id>/<work_record_id>.jsonl``, one envelope per line."""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve(strict=False)

    @property
    def root(self) -> Path:
        return self._root

    def append(
        self,
        *,
        contract_id: str,
        work_record_id: str,
        sender_agent_id: str,
        channel_id: str,
        kind: MessageKind,
        body: str,
        references: list[MessageReference],
        now: datetime | None = None,
        recipient_agent_id: str | None = None,
        recipient_role: str | None = None,
        verdict: Literal["block", "clear"] | None = None,
        head_sha: str | None = None,
    ) -> MessageEnvelope:
        """Store one envelope. Raises ``PermissionError`` on a sealed Contract.

        A `result` that names no addressee answers the `message` it references, so its
        recipient is that Message's sender (PRD issue 53: `result` wakes the requester).
        """

        self._require_open(contract_id)
        if kind is MessageKind.RESULT and not recipient_agent_id and not recipient_role:
            recipient_agent_id = self._requester_of(contract_id, work_record_id, references)
        envelope = MessageEnvelope(
            message_id=uuid4(),
            sender_agent_id=UUID(sender_agent_id),
            channel_id=UUID(channel_id),
            work_record_id=UUID(work_record_id),
            kind=kind,
            references=list(references),
            size_bytes=len(body.encode("utf-8")),
            sent_at=now or datetime.now(UTC),
            recipient_agent_id=UUID(recipient_agent_id) if recipient_agent_id else None,
            recipient_role=recipient_role or None,
            verdict=verdict,
            head_sha=head_sha or None,
            body=body,
        )
        path = self._path(contract_id, work_record_id)
        _ensure_dir(path.parent)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(envelope.model_dump_json() + "\n")
        path.chmod(_FILE_MODE)
        return envelope

    def read(
        self, *, contract_id: str, work_record_id: str, channel_id: str | None = None
    ) -> list[MessageEnvelope]:
        """The Conversation, or one Channel of it. Raises ``PermissionError`` when sealed."""

        self._require_open(contract_id)
        return self._read(contract_id, work_record_id, channel_id)

    def depth(self, *, contract_id: str, work_record_id: str, channel_id: str) -> int:
        return len(self._read(contract_id, work_record_id, channel_id))

    def pending(
        self,
        *,
        contract_id: str,
        work_record_id: str,
        channel_id: str,
        agent_id: str,
        roles: tuple[str, ...] = (),
    ) -> list[MessageEnvelope]:
        """What one member has not yet seen: every Message on the Channel after its
        cursor that is addressed to it -- by id, by a role it holds, or to nobody in
        particular (a `note` to the Swarm) -- and not its own (PRD issue 53)."""

        self._require_open(contract_id)
        cursor = self._cursors(contract_id, work_record_id).get(agent_id, "")
        envelopes = self._read(contract_id, work_record_id, channel_id)
        unseen = _after(envelopes, cursor)
        me = UUID(agent_id)
        return [
            envelope
            for envelope in unseen
            if envelope.sender_agent_id != me
            and (
                envelope.recipient_agent_id == me
                or (envelope.recipient_role is not None and envelope.recipient_role in roles)
                or (envelope.recipient_agent_id is None and envelope.recipient_role is None)
            )
        ]

    def mark_consumed(
        self, *, contract_id: str, work_record_id: str, agent_id: str, through: MessageEnvelope
    ) -> None:
        """Advance one member's cursor: the Directive that folded ``through`` in ran."""

        cursors = self._cursors(contract_id, work_record_id)
        cursors[agent_id] = str(through.message_id)
        path = self._cursor_path(contract_id, work_record_id)
        _ensure_dir(path.parent)
        path.write_text(json.dumps(cursors), encoding="utf-8")
        path.chmod(_FILE_MODE)

    def transcript(self, request: TranscriptRequest) -> TranscriptDelivery:
        """The pull-through's answer: every body, or the Contract state that seals them."""

        contract_id = str(request.contract_id)
        sealed = self.sealed_state(contract_id)
        if sealed is not None:
            return TranscriptDelivery(
                request_id=request.request_id,
                work_record_id=request.work_record_id,
                refused=sealed,
            )
        return TranscriptDelivery(
            request_id=request.request_id,
            work_record_id=request.work_record_id,
            messages=self._read(contract_id, str(request.work_record_id), None),
        )

    # ------------------------------------------------------------- lifecycle

    def seal(self, contract_id: str, *, contract_state: str) -> int:
        """Seal every store of the Contract (map ticket 10): kept, unreadable, until retention.

        Returns how many Work Record stores the seal covers. Idempotent, and a Contract
        with no store gets no marker -- there is nothing to retain.
        """

        contract_dir = self._root / contract_id
        stores = _stores(contract_dir)
        if not stores:
            return 0
        marker = contract_dir / SEALED_MARKER
        marker.write_text(json.dumps({"contract_state": contract_state}), encoding="utf-8")
        marker.chmod(_FILE_MODE)
        return len(stores)

    def sealed_state(self, contract_id: str) -> str | None:
        marker = self._root / contract_id / SEALED_MARKER
        if not marker.is_file():
            return None
        try:
            loaded = json.loads(marker.read_text(encoding="utf-8"))
        except ValueError:
            return "terminated"
        state = loaded.get("contract_state") if isinstance(loaded, Mapping) else None
        return str(state) if state else "terminated"

    def held(self) -> list[tuple[str, str]]:
        """Every ``(contract_id, work_record_id)`` with a store on this Runner."""

        if not self._root.is_dir():
            return []
        return [
            (contract_dir.name, store.stem)
            for contract_dir in sorted(self._root.iterdir())
            if contract_dir.is_dir()
            for store in _stores(contract_dir)
        ]

    def remove(self, contract_id: str, work_record_id: str) -> bool:
        """The retention drop. A Contract whose last store went takes its seal with it."""

        path = self._path(contract_id, work_record_id)
        if not path.is_file():
            return False
        path.unlink()
        self._cursor_path(contract_id, work_record_id).unlink(missing_ok=True)
        contract_dir = path.parent
        if not _stores(contract_dir):
            shutil.rmtree(contract_dir, ignore_errors=True)
        return True

    # ------------------------------------------------------------- internals

    def _require_open(self, contract_id: str) -> None:
        sealed = self.sealed_state(contract_id)
        if sealed is not None:
            raise PermissionError(
                f"the Message store of Contract {contract_id} is sealed ({sealed})"
            )

    def _path(self, contract_id: str, work_record_id: str) -> Path:
        # Both segments are platform ids (UUIDs) by the time they reach here; refusing
        # anything else keeps a path from escaping the root.
        return self._root / str(UUID(contract_id)) / f"{UUID(work_record_id)}.jsonl"

    def _cursor_path(self, contract_id: str, work_record_id: str) -> Path:
        return self._path(contract_id, work_record_id).with_suffix(".cursors.json")

    def _cursors(self, contract_id: str, work_record_id: str) -> dict[str, str]:
        path = self._cursor_path(contract_id, work_record_id)
        if not path.is_file():
            return {}
        loaded = json.loads(path.read_text(encoding="utf-8"))
        return {str(k): str(v) for k, v in loaded.items()} if isinstance(loaded, Mapping) else {}

    def _requester_of(
        self, contract_id: str, work_record_id: str, references: list[MessageReference]
    ) -> str | None:
        answered = {ref.value for ref in references if ref.kind is ReferenceKind.MESSAGE}
        if not answered:
            return None
        return next(
            (
                str(envelope.sender_agent_id)
                for envelope in self._read(contract_id, work_record_id, None)
                if str(envelope.message_id) in answered
            ),
            None,
        )

    def _read(
        self, contract_id: str, work_record_id: str, channel_id: str | None
    ) -> list[MessageEnvelope]:
        path = self._path(contract_id, work_record_id)
        if not path.is_file():
            return []
        envelopes = [
            MessageEnvelope.model_validate_json(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        if channel_id is None:
            return envelopes
        wanted = UUID(channel_id)
        return [envelope for envelope in envelopes if envelope.channel_id == wanted]


def _stores(contract_dir: Path) -> list[Path]:
    if not contract_dir.is_dir():
        return []
    return sorted(entry for entry in contract_dir.iterdir() if entry.suffix == ".jsonl")


def _after(envelopes: list[MessageEnvelope], cursor: str) -> list[MessageEnvelope]:
    if not cursor:
        return envelopes
    for index, envelope in enumerate(envelopes):
        if str(envelope.message_id) == cursor:
            return envelopes[index + 1 :]
    return envelopes


def _ensure_dir(contract_dir: Path) -> None:
    """Root and Contract directory alike: the Runner's own, closed to every other uid."""

    for directory in (contract_dir.parent, contract_dir):
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, _DIR_MODE)
