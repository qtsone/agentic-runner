"""User-connected Sources, read on the Runner their owner hosts (PRD issue 50, map 16 A3).

The owner's scenario: a contractor whose employer is not on the platform, running this
Runner on their own laptop, paying their own key. Their mailbox (or Slack identity) is
read *here*, with a credential that lives only in this host's store, and nothing reaches
the control plane unless the Product's Intake Lead decides it is work:

1. **Poll** each Source the last heartbeat ack assigned, with the value its Credential
   Reference names -- read from the host store at poll time and never logged.
2. **Filter locally** (16 A4) -- folders, senders, the addressing rule -- before any token.
3. **Triage** the survivors of one poll as *one* Directive (16 A6: the way a person reads
   a channel in a sweep), through this Runner's own LLM proxy, so the one Usage Record it
   meters rides the heartbeat like any other.
4. ``ignore`` sends an ids-only report; ``own``/``ask`` go through the create-Work-Record
   seam (16 A10). With the Source's reply toggle on, the Runner answers in the thread with
   the owner's own credential (16 A5); off, it says nothing and the owner is asked.

The connectors keep a per-Source high-water mark in memory. It is the connector's cursor,
not an activity's state (ADR-0013 §3 is about activities); a restart re-reads what is still
unseen and the control plane's idempotency key lands a re-sent message on its first record.
"""

from __future__ import annotations

import asyncio
import contextlib
import email
import imaplib
import logging
import smtplib
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.policy import default as default_policy
from email.utils import getaddresses, make_msgid, parseaddr
from typing import Any, Final, Protocol
from uuid import UUID, uuid4

import httpx

from agentic_runner.credentials import HostCredentialStore
from agentic_runner.llm_proxy import LlmProxy
from agentic_runner.triage_activities import run_triage_turn
from agentic_runner_contracts.triage import TriageCandidate, TriageDecision, build_prompt, decide
from agentic_runner_contracts.user_sources import (
    MEMBER_INSTALLS_DISABLED,
    IntakeIgnoredReport,
    IntakeWorkRequest,
    IntakeWorkResponse,
    SourceIntakeFilter,
    SourceStatus,
    UserSourceAssignment,
    UserSourceKind,
    message_ref,
)

__all__ = [
    "SLACK_MEMBER_INSTALL_REFUSALS",
    "ImapMailbox",
    "InboundMessage",
    "IntakeStream",
    "ProxyTriage",
    "SlackUserConnector",
    "SourceConnectRefusedError",
    "SourceConnector",
    "UserSourcePoller",
    "passes_filter",
]

_logger = logging.getLogger(__name__)

# The Slack errors a workspace that forbids member app installs answers a user token
# with. Every one of them means the same thing to the owner -- this workspace will not
# let you connect yourself -- so they collapse into the one documented console message.
SLACK_MEMBER_INSTALL_REFUSALS: Final = frozenset(
    {"restricted_action", "app_restricted", "access_denied", "team_access_not_granted"}
)
_DESCRIPTION_MAX: Final = 4000
_SLACK_API: Final = "https://slack.com/api"


class SourceConnectRefusedError(RuntimeError):
    """The Source could not be connected; ``code`` is what the console is told."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class InboundMessage:
    message_id: str
    sender: str
    to: tuple[str, ...]
    cc: tuple[str, ...] = ()
    subject: str = ""
    body: str = ""
    folder: str = "INBOX"
    # Slack only: the thread a reply goes into.
    thread: str | None = None


class SourceConnector(Protocol):
    async def connect(self) -> None: ...

    async def fetch_new(self) -> list[InboundMessage]: ...

    async def reply(self, message: InboundMessage, text: str) -> None: ...


class IntakeStream(Protocol):
    """The two outbound calls, signed with the Runner identity (``registration``)."""

    async def create_work_record(self, request: IntakeWorkRequest) -> IntakeWorkResponse: ...

    async def report_ignored(self, report: IntakeIgnoredReport) -> None: ...


Triage = Callable[
    [UserSourceAssignment, Sequence[InboundMessage]], Awaitable[tuple[TriageDecision, str]]
]
ConnectorFactory = Callable[[UserSourceAssignment, str], SourceConnector]


def passes_filter(
    intake_filter: SourceIntakeFilter, message: InboundMessage, *, address: str
) -> bool:
    """The Intake Filter, before any token is spent (16 A4). Folders are the fetch's own
    scope; here are the senders and the addressing rule."""

    me = address.strip().lower()
    senders = {sender.strip().lower() for sender in intake_filter.sender_ids}
    if senders and message.sender.strip().lower() not in senders:
        return False
    if message.folder not in intake_filter.folders:
        return False
    to = {recipient.lower() for recipient in message.to}
    cc = {recipient.lower() for recipient in message.cc}
    rule = intake_filter.addressing_rule
    if rule == "everything":
        return True
    if rule == "addressed_to_me":
        return me in to
    if rule == "mentions_me":
        return me in to or me in cc
    # ponytail: `replies_in_my_threads` needs the owner's sent mail to know which threads
    # are theirs; it never passes rather than guess, as on the org-installed Source.
    return False


# ----------------------------------------------------------------------------- email


class ImapMailbox:
    """The owner's mailbox over IMAP (read) and SMTP (reply), with their own credential.

    Read-only on the mailbox: messages are fetched with ``BODY.PEEK`` from folders opened
    read-only, so a message the Runner has triaged is still unread for its owner.
    """

    def __init__(
        self,
        assignment: UserSourceAssignment,
        password: str,
        *,
        imap_factory: Callable[[str, int], Any] | None = None,
        smtp_factory: Callable[[str, int], Any] | None = None,
    ) -> None:
        if not assignment.imap_host:
            raise SourceConnectRefusedError("connect_failed", "the Source has no IMAP host")
        self._assignment = assignment
        self._password = password
        self._imap_factory = imap_factory or imaplib.IMAP4_SSL
        self._smtp_factory = smtp_factory or smtplib.SMTP_SSL
        self._high_water: dict[str, int] = {}

    def __repr__(self) -> str:
        # Never the password, whoever formats this object.
        return f"ImapMailbox({self._assignment.address!r})"

    async def connect(self) -> None:
        await asyncio.to_thread(self._with_session, lambda _conn: None)

    async def fetch_new(self) -> list[InboundMessage]:
        return await asyncio.to_thread(self._with_session, self._fetch)

    async def reply(self, message: InboundMessage, text: str) -> None:
        await asyncio.to_thread(self._send_reply, message, text)

    def _with_session[T](self, action: Callable[[Any], T]) -> T:
        assignment = self._assignment
        assert assignment.imap_host is not None
        try:
            conn = self._imap_factory(assignment.imap_host, assignment.imap_port)
        except OSError as error:
            raise SourceConnectRefusedError("connect_failed", type(error).__name__) from error
        try:
            try:
                conn.login(assignment.address, self._password)
            except imaplib.IMAP4.error as error:
                # The server's own words can echo what was sent; only the type goes on.
                raise SourceConnectRefusedError("connect_failed", "IMAP login refused") from error
            return action(conn)
        finally:
            with contextlib.suppress(imaplib.IMAP4.error, OSError):
                conn.logout()

    def _fetch(self, conn: Any) -> list[InboundMessage]:
        messages: list[InboundMessage] = []
        for folder in self._assignment.intake_filter.folders:
            status, _ = conn.select(_quote(folder), readonly=True)
            if status != "OK":
                continue
            high = self._high_water.get(folder, 0)
            status, data = conn.uid("SEARCH", None, f"UID {high + 1}:* UNSEEN")
            if status != "OK" or not data or not data[0]:
                continue
            uids = sorted(int(uid) for uid in data[0].split() if int(uid) > high)
            for uid in uids:
                status, fetched = conn.uid("FETCH", str(uid), "(BODY.PEEK[])")
                raw = _fetched_bytes(fetched) if status == "OK" else None
                if raw is not None:
                    messages.append(_parse_email(raw, folder=folder))
            if uids:
                self._high_water[folder] = uids[-1]
        return messages

    def _send_reply(self, message: InboundMessage, text: str) -> None:
        assignment = self._assignment
        reply = EmailMessage()
        reply["From"] = assignment.address
        reply["To"] = message.sender
        subject = message.subject or ""
        reply["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
        reply["Message-ID"] = make_msgid()
        # The thread is the original's Message-ID: every client threads on these two.
        reply["In-Reply-To"] = message.message_id
        reply["References"] = message.message_id
        reply.set_content(text)
        host = assignment.smtp_host or assignment.imap_host
        assert host is not None
        with self._smtp_factory(host, assignment.smtp_port) as smtp:
            smtp.login(assignment.address, self._password)
            smtp.send_message(reply)


def _quote(folder: str) -> str:
    return f'"{folder}"' if " " in folder else folder


def _fetched_bytes(fetched: Sequence[Any]) -> bytes | None:
    for part in fetched:
        if isinstance(part, tuple) and len(part) >= 2 and isinstance(part[1], bytes):
            return part[1]
    return None


def _parse_email(raw: bytes, *, folder: str) -> InboundMessage:
    parsed = email.message_from_bytes(raw, policy=default_policy)
    body_part = parsed.get_body(preferencelist=("plain",)) if parsed.is_multipart() else parsed
    body = body_part.get_content() if body_part is not None else ""
    return InboundMessage(
        message_id=str(parsed.get("Message-ID") or f"<{uuid4()}@runner.invalid>").strip(),
        sender=parseaddr(str(parsed.get("From") or ""))[1],
        to=tuple(address for _, address in getaddresses(parsed.get_all("To", []))),
        cc=tuple(address for _, address in getaddresses(parsed.get_all("Cc", []))),
        subject=str(parsed.get("Subject") or ""),
        body=body if isinstance(body, str) else "",
        folder=folder,
    )


# ----------------------------------------------------------------------------- slack


class SlackUserApi(Protocol):
    async def call(self, method: str, params: Mapping[str, str]) -> dict[str, Any]: ...


@dataclass
class HttpSlackUserApi:
    """Slack's Web API under the owner's *user* token -- messages go out as them."""

    token: str
    base_url: str = _SLACK_API

    def __repr__(self) -> str:
        return "HttpSlackUserApi(<token>)"

    async def call(self, method: str, params: Mapping[str, str]) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{self.base_url}/{method}",
                data=dict(params),
                headers={"authorization": f"Bearer {self.token}"},
            )
        payload: dict[str, Any] = response.json()
        return payload


class SlackUserConnector:
    """The owner's own Slack identity: their direct messages are what is addressed to them.

    Connected only where the workspace allows member installs; a workspace that does not
    refuses the user token, and the connector says so as :data:`MEMBER_INSTALLS_DISABLED`.
    """

    def __init__(self, assignment: UserSourceAssignment, api: SlackUserApi) -> None:
        self._assignment = assignment
        self._api = api
        self._me = assignment.address.rpartition("/")[2]
        self._oldest: dict[str, str] = {}

    async def connect(self) -> None:
        answer = await self._api.call("auth.test", {})
        if answer.get("ok"):
            self._me = str(answer.get("user_id") or self._me)
            return
        error = str(answer.get("error") or "")
        if error in SLACK_MEMBER_INSTALL_REFUSALS:
            raise SourceConnectRefusedError(
                MEMBER_INSTALLS_DISABLED, "the workspace does not allow member installs"
            )
        raise SourceConnectRefusedError("connect_failed", f"auth.test refused: {error}")

    async def fetch_new(self) -> list[InboundMessage]:
        listed = await self._api.call("conversations.list", {"types": "im"})
        messages: list[InboundMessage] = []
        for channel in listed.get("channels") or []:
            channel_id = str(channel.get("id"))
            params = {"channel": channel_id}
            if channel_id in self._oldest:
                params["oldest"] = self._oldest[channel_id]
            history = await self._api.call("conversations.history", params)
            for item in reversed(history.get("messages") or []):
                ts = str(item.get("ts"))
                if ts == self._oldest.get(channel_id) or item.get("user") == self._me:
                    continue
                messages.append(
                    InboundMessage(
                        message_id=f"{channel_id}:{ts}",
                        sender=str(item.get("user") or ""),
                        # A direct message is addressed to its owner by construction,
                        # named the way the Source names them for the Intake Filter.
                        to=(self._assignment.address,),
                        body=str(item.get("text") or ""),
                        folder="INBOX",
                        thread=str(item.get("thread_ts") or ts),
                    )
                )
                self._oldest[channel_id] = ts
        return messages

    async def reply(self, message: InboundMessage, text: str) -> None:
        channel_id = message.message_id.partition(":")[0]
        await self._api.call(
            "chat.postMessage",
            {"channel": channel_id, "text": text, "thread_ts": message.thread or ""},
        )


def default_connector(assignment: UserSourceAssignment, secret: str) -> SourceConnector:
    if assignment.kind is UserSourceKind.SLACK_USER:
        return SlackUserConnector(assignment, HttpSlackUserApi(token=secret))
    return ImapMailbox(assignment, secret)


# ----------------------------------------------------------------------------- triage


@dataclass
class ProxyTriage:
    """The Triage Directive's one turn, through this Runner's LLM proxy.

    Funded on the Intake Lead's Contract and metered by the proxy like any Directive, so
    it leaves exactly one Usage Record -- ``work_record_id`` empty, there is none yet. A
    hang, a refusal or a malformed answer is ``None``, which :func:`decide` answers with
    the keyword fallback (never-guess: what it cannot place is ``ask``).
    """

    proxy: LlmProxy
    model: str
    timeout_seconds: float
    client: httpx.AsyncClient | None = None

    async def __call__(
        self, assignment: UserSourceAssignment, messages: Sequence[InboundMessage]
    ) -> tuple[TriageDecision, str]:
        directive_id = f"triage:{uuid4()}"
        candidates = [
            TriageCandidate(agent_id=c.agent_id, specialisation=c.specialisation)
            for c in assignment.candidates
        ]
        text = describe(messages)
        prompt = build_prompt(
            message_text=text, intake_brief=assignment.intake_brief, candidates=candidates
        )
        raw = await self._run(assignment, directive_id=directive_id, prompt=prompt)
        return decide(raw, text, candidates=candidates), directive_id

    async def _run(
        self, assignment: UserSourceAssignment, *, directive_id: str, prompt: str
    ) -> str | None:
        return await run_triage_turn(
            self.proxy,
            model=self.model,
            timeout_seconds=self.timeout_seconds,
            prompt=prompt,
            directive_id=directive_id,
            contract_id=assignment.intake_lead_contract_id,
            agent_id=assignment.intake_lead_agent_id,
            client=self.client,
        )


def describe(messages: Sequence[InboundMessage]) -> str:
    """One poll's survivors as one description, bounded to what a Work Record takes."""

    parts = []
    for message in messages:
        head = [f"From: {message.sender}"]
        if message.subject:
            head.append(f"Subject: {message.subject}")
        parts.append("\n".join([*head, message.body.strip()]))
    text = "\n\n---\n\n".join(parts).strip() or "(empty message)"
    return text[:_DESCRIPTION_MAX]


# ----------------------------------------------------------------------------- the poller


@dataclass
class UserSourcePoller:
    """Every assigned Source, one poll at a time: filter, triage once, act on the outcome."""

    stream: IntakeStream
    store: HostCredentialStore
    triage: Triage
    connector_factory: ConnectorFactory = default_connector
    _assignments: list[UserSourceAssignment] = field(default_factory=list)
    _connectors: dict[UUID, tuple[UserSourceAssignment, SourceConnector]] = field(
        default_factory=dict
    )
    _status: dict[UUID, SourceStatus] = field(default_factory=dict)

    def assign(self, assignments: Sequence[UserSourceAssignment]) -> None:
        """What the last ack pushed. Authoritative: a Source not in it is dropped."""

        self._assignments = list(assignments)
        live = {assignment.source_id for assignment in assignments}
        for source_id in list(self._connectors):
            if source_id not in live:
                del self._connectors[source_id]
        self._status = {k: v for k, v in self._status.items() if k in live}

    def statuses(self) -> list[SourceStatus]:
        return list(self._status.values())

    async def poll(self) -> None:
        for assignment in list(self._assignments):
            try:
                await self.poll_one(assignment)
            except Exception as error:  # noqa: BLE001 - one Source never stops the others
                _logger.warning(
                    "user-connected Source %s poll failed: %s",
                    assignment.source_id,
                    type(error).__name__,
                )

    async def poll_one(self, assignment: UserSourceAssignment) -> None:
        connector = await self._connector(assignment)
        if connector is None:
            return
        try:
            messages = await connector.fetch_new()
        except SourceConnectRefusedError as error:
            self._refused(assignment, error.code)
            return
        self._status[assignment.source_id] = SourceStatus(
            source_id=assignment.source_id, connected=True
        )
        survivors = [
            message
            for message in messages
            if passes_filter(assignment.intake_filter, message, address=assignment.address)
        ]
        if not survivors:
            return
        decision, directive_id = await self.triage(assignment, survivors)
        refs = [message_ref(message.message_id) for message in survivors]
        if decision.outcome == "ignore":
            await self.stream.report_ignored(
                IntakeIgnoredReport(
                    source_id=assignment.source_id,
                    product_id=assignment.product_id,
                    directive_id=directive_id,
                    classification_source=decision.source,
                    message_refs=refs,
                )
            )
            return
        await self.stream.create_work_record(
            IntakeWorkRequest(
                source_id=assignment.source_id,
                product_id=assignment.product_id,
                directive_id=directive_id,
                outcome=decision.outcome,
                classification_source=decision.source,
                reason=decision.reason[:500],
                lead_agent_id=decision.lead_agent_id,
                description=describe(survivors),
                requester=survivors[0].sender or assignment.address,
                message_refs=refs,
            )
        )
        if assignment.reply_enabled:
            text = (
                f"Taken -- routed to a Lead. {decision.reason}"
                if decision.outcome == "own"
                else f"I need a hand with this: {decision.reason}"
            )
            await connector.reply(survivors[0], text)

    async def _connector(self, assignment: UserSourceAssignment) -> SourceConnector | None:
        held = self._connectors.get(assignment.source_id)
        if held is not None and held[0] == assignment:
            return held[1]
        secret = self.store.get(assignment.credential_reference)
        if secret is None:
            self._refused(assignment, "credential_missing")
            return None
        try:
            connector = self.connector_factory(assignment, secret)
            await connector.connect()
        except SourceConnectRefusedError as error:
            self._refused(assignment, error.code)
            return None
        self._connectors[assignment.source_id] = (assignment, connector)
        return connector

    def _refused(self, assignment: UserSourceAssignment, code: str) -> None:
        self._connectors.pop(assignment.source_id, None)
        self._status[assignment.source_id] = SourceStatus(
            source_id=assignment.source_id, connected=False, error_code=code
        )
        _logger.warning("user-connected Source %s not connected: %s", assignment.source_id, code)
