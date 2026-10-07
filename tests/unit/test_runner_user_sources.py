"""User-connected Sources on the Runner (PRD issue 50, map ticket 16 A3-A6).

`.scratch/multi-org-release-1/issues/50-user-connected-sources-email-slack-identity.md`.

The mailbox is a real IMAP conversation: a small in-process IMAP server on a loopback
socket, driven by the connector's own ``imaplib`` code, so what is pinned is what the
Runner actually sends -- the LOGIN carrying the value the host store holds under the
Credential Reference, ``BODY.PEEK`` so the owner's mail stays unread, the Intake Filter
applied before any token. The Triage Directive runs through this Runner's real LLM proxy
against a fake provider, so the Usage Record an ``ignore`` leaves is the proxy's own.
"""

from __future__ import annotations

import imaplib
import json
import logging
import socketserver
import threading
from collections.abc import Iterator, Mapping, Sequence
from email.message import EmailMessage
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from agentic_runner.credentials import FakeCredentialStore
from agentic_runner.llm_proxy import CredentialSlot, LlmProxy, SlotStore
from agentic_runner.user_sources import (
    ImapMailbox,
    InboundMessage,
    ProxyTriage,
    SlackUserConnector,
    SourceConnectRefusedError,
    UserSourcePoller,
)
from agentic_runner_contracts.triage import TriageDecision
from agentic_runner_contracts.user_sources import (
    MEMBER_INSTALLS_DISABLED,
    IntakeIgnoredReport,
    IntakeWorkRequest,
    IntakeWorkResponse,
    SourceIntakeFilter,
    TriageCandidateRef,
    UserSourceAssignment,
    UserSourceKind,
)

ME = "dana@acme.example"
ALICE = "alice@acme.example"
BOB = "bob@acme.example"
REFERENCE = "acme-mailbox"
SECRET = "app-password-9f3c1e"


# ----------------------------------------------------------------------------- fakes


def _email(*, message_id: str, sender: str, to: str, cc: str = "", body: str) -> bytes:
    message = EmailMessage()
    message["Message-ID"] = message_id
    message["From"] = sender
    message["To"] = to
    if cc:
        message["Cc"] = cc
    message["Subject"] = "the readiness page"
    message.set_content(body)
    return bytes(message)


class FakeImapServer(socketserver.ThreadingTCPServer):
    """Just enough IMAP4rev1 for ``imaplib``: LOGIN, EXAMINE/SELECT, UID SEARCH/FETCH."""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, messages: dict[str, list[bytes]]) -> None:
        super().__init__(("127.0.0.1", 0), _ImapHandler)
        self.folders = messages
        self.logins: list[tuple[str, str]] = []
        self.commands: list[str] = []

    @property
    def port(self) -> int:
        return int(self.server_address[1])


class _ImapHandler(socketserver.StreamRequestHandler):
    server: FakeImapServer

    def _send(self, line: str) -> None:
        self.wfile.write(line.encode("utf-8") + b"\r\n")

    def handle(self) -> None:
        self._send("* OK fake IMAP4rev1 ready")
        folder: list[bytes] = []
        while raw := self.rfile.readline():
            tag, _, rest = raw.decode("utf-8").rstrip("\r\n").partition(" ")
            command, _, args = rest.partition(" ")
            command = command.upper()
            self.server.commands.append(f"{command} {args}" if command != "LOGIN" else command)
            if command == "CAPABILITY":
                self._send("* CAPABILITY IMAP4rev1")
            elif command == "LOGIN":
                user, password = (part.strip('"') for part in args.split(" ", 1))
                self.server.logins.append((user, password))
            elif command in {"SELECT", "EXAMINE"}:
                folder = self.server.folders.get(args.strip('"'), [])
                self._send(f"* {len(folder)} EXISTS")
            elif command == "UID" and args.upper().startswith("SEARCH"):
                self._send("* SEARCH " + " ".join(str(i + 1) for i in range(len(folder))))
            elif command == "UID" and args.upper().startswith("FETCH"):
                uid = int(args.split()[1])
                body = folder[uid - 1]
                self.wfile.write(f"* {uid} FETCH (UID {uid} BODY[] {{{len(body)}}}\r\n".encode())
                self.wfile.write(body + b")\r\n")
            elif command == "LOGOUT":
                self._send("* BYE")
                self._send(f"{tag} OK LOGOUT completed")
                return
            self._send(f"{tag} OK {command} completed")


@pytest.fixture
def imap() -> Iterator[FakeImapServer]:
    server = FakeImapServer(
        {
            "INBOX": [
                _email(
                    message_id="<1@acme>", sender=ALICE, to=ME, body="the readiness page is down"
                ),
                # Cc'd, not addressed: `addressed_to_me` keeps it out.
                _email(message_id="<2@acme>", sender=ALICE, to=BOB, cc=ME, body="fyi the standup"),
                # Addressed, but not from the one sender the filter admits.
                _email(message_id="<3@acme>", sender=BOB, to=ME, body="lunch?"),
            ]
        }
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


class FakeSmtp:
    """The mailbox's outbound half: what an SMTP reply would have delivered."""

    sent: list[EmailMessage] = []

    def __init__(self, host: str, port: int) -> None:
        self.logins: list[tuple[str, str]] = []

    def __enter__(self) -> FakeSmtp:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def login(self, user: str, password: str) -> None:
        self.logins.append((user, password))

    def send_message(self, message: EmailMessage) -> None:
        FakeSmtp.sent.append(message)


class RecordingStream:
    def __init__(self) -> None:
        self.created: list[IntakeWorkRequest] = []
        self.ignored: list[IntakeIgnoredReport] = []

    async def create_work_record(self, request: IntakeWorkRequest) -> IntakeWorkResponse:
        self.created.append(request)
        return IntakeWorkResponse(work_record_id=uuid4(), notified_owner=True)

    async def report_ignored(self, report: IntakeIgnoredReport) -> None:
        self.ignored.append(report)


class FixedTriage:
    def __init__(self, decision: TriageDecision) -> None:
        self.decision = decision
        self.seen: list[list[InboundMessage]] = []

    async def __call__(
        self, assignment: UserSourceAssignment, messages: Sequence[InboundMessage]
    ) -> tuple[TriageDecision, str]:
        self.seen.append(list(messages))
        return self.decision, "triage:fixed"


def _assignment(
    port: int = 993,
    *,
    reply_enabled: bool = False,
    kind: UserSourceKind = UserSourceKind.EMAIL,
    address: str = ME,
    lead: UUID | None = None,
) -> UserSourceAssignment:
    return UserSourceAssignment(
        source_id=UUID(int=1),
        product_id=UUID(int=2),
        kind=kind,
        address=address,
        credential_reference=REFERENCE,
        imap_host="127.0.0.1",
        imap_port=port,
        smtp_host="127.0.0.1",
        smtp_port=465,
        intake_filter=SourceIntakeFilter(sender_ids=[ALICE]),
        reply_enabled=reply_enabled,
        intake_lead_agent_id=UUID(int=3),
        intake_lead_contract_id=UUID(int=4),
        candidates=[TriageCandidateRef(agent_id=lead or UUID(int=5), specialisation="development")],
    )


def _mailbox_factory(assignment: UserSourceAssignment, secret: str) -> ImapMailbox:
    return ImapMailbox(
        assignment,
        secret,
        imap_factory=lambda host, port: imaplib.IMAP4(host, port),
        smtp_factory=FakeSmtp,
    )


def _poller(stream: RecordingStream, triage: Any) -> UserSourcePoller:
    return UserSourcePoller(
        stream=stream,
        store=FakeCredentialStore({REFERENCE: SECRET}),
        triage=triage,
        connector_factory=_mailbox_factory,
    )


# ----------------------------------------------------------------------------- the filter


@pytest.mark.asyncio
async def test_three_messages_one_survivor_reaches_the_triage_directive(
    imap: FakeImapServer, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    triage = FixedTriage(TriageDecision("ignore", None, "chatter", "llm"))
    poller = _poller(RecordingStream(), triage)
    poller.assign([_assignment(imap.port)])

    await poller.poll()

    assert len(triage.seen) == 1, "one poll's survivors are one Directive"
    [survivors] = triage.seen
    assert [message.message_id for message in survivors] == ["<1@acme>"]
    # The credential came from the host store, by reference, and went to the server only.
    assert imap.logins and all(login == (ME, SECRET) for login in imap.logins)
    assert all(SECRET not in record.getMessage() for record in caplog.records)
    assert SECRET not in repr(poller.connector_factory(_assignment(imap.port), SECRET))
    # The owner's mail stays unread: opened read-only, fetched with PEEK.
    assert any(command.startswith("EXAMINE") for command in imap.commands)
    assert all("PEEK" in command for command in imap.commands if "FETCH" in command)
    assert [status.connected for status in poller.statuses()] == [True]


@pytest.mark.asyncio
async def test_a_missing_credential_is_reported_as_a_code_and_reads_nothing(
    imap: FakeImapServer,
) -> None:
    triage = FixedTriage(TriageDecision("ignore", None, "chatter", "llm"))
    poller = UserSourcePoller(
        stream=RecordingStream(),
        store=FakeCredentialStore({}),
        triage=triage,
        connector_factory=_mailbox_factory,
    )
    poller.assign([_assignment(imap.port)])

    await poller.poll()

    assert imap.logins == []
    assert triage.seen == []
    [status] = poller.statuses()
    assert (status.connected, status.error_code) == (False, "credential_missing")


# ----------------------------------------------------------------------------- outcomes


class FakeProvider:
    """The funder's provider behind the proxy: one completion carrying the label."""

    def __init__(self, label: dict[str, object]) -> None:
        self.label = label
        self.calls = 0

    def transport(self) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/models"):
                return httpx.Response(200, json={"data": []})
            self.calls += 1
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": json.dumps(self.label)}}],
                    "usage": {"prompt_tokens": 120, "completion_tokens": 9},
                },
            )

        return httpx.MockTransport(handle)


async def _proxy_with_slot(provider: FakeProvider) -> LlmProxy:
    async def always_valid(_slot: CredentialSlot) -> bool:
        return True

    slots = SlotStore(probe=always_valid)
    await slots.put(
        UUID(int=4),
        CredentialSlot(
            reference="funder-key",
            key_id="k1",
            provider_name="openai",
            base_url="https://provider.invalid/v1",
            value="sk-funder",
        ),
    )
    return LlmProxy(slots=slots, client=httpx.AsyncClient(transport=provider.transport()))


@pytest.mark.asyncio
async def test_ignore_leaves_one_usage_record_and_one_ids_only_report(
    imap: FakeImapServer,
) -> None:
    provider = FakeProvider({"outcome": "ignore", "lead_agent_id": None, "reason": "chatter"})
    stream = RecordingStream()
    async with await _proxy_with_slot(provider) as proxy:
        poller = _poller(stream, ProxyTriage(proxy=proxy, model="gpt-5-mini", timeout_seconds=5))
        poller.assign([_assignment(imap.port)])
        await poller.poll()
        usage = proxy.outbox.pending()

    assert provider.calls == 1
    [record] = usage
    assert record.work_record_id is None
    assert (record.agent_id, record.contract_id) == (UUID(int=3), UUID(int=4))
    assert record.directive_id.startswith("triage:")

    assert stream.created == []
    [report] = stream.ignored
    # The schema itself has no place for a body, and forbids one arriving as an extra.
    assert set(IntakeIgnoredReport.model_fields) == {
        "source_id",
        "product_id",
        "directive_id",
        "classification_source",
        "message_refs",
    }
    assert IntakeIgnoredReport.model_config["extra"] == "forbid"
    outbound = report.model_dump_json()
    assert "readiness" not in outbound and "<1@acme>" not in outbound
    assert report.directive_id == record.directive_id


@pytest.mark.asyncio
async def test_own_is_one_create_work_record_call_naming_the_lead(imap: FakeImapServer) -> None:
    lead = uuid4()
    FakeSmtp.sent = []
    provider = FakeProvider({"outcome": "own", "lead_agent_id": str(lead), "reason": "dev work"})
    stream = RecordingStream()
    async with await _proxy_with_slot(provider) as proxy:
        poller = _poller(stream, ProxyTriage(proxy=proxy, model="gpt-5-mini", timeout_seconds=5))
        poller.assign([_assignment(imap.port, lead=lead)])
        await poller.poll()

    assert stream.ignored == []
    [request] = stream.created
    assert (request.outcome, request.lead_agent_id) == ("own", lead)
    assert request.requester == ALICE
    assert "the readiness page is down" in request.description
    assert FakeSmtp.sent == []


@pytest.mark.asyncio
async def test_ask_with_reply_off_sends_nothing_into_the_mailbox(imap: FakeImapServer) -> None:
    FakeSmtp.sent = []
    stream = RecordingStream()
    poller = _poller(stream, FixedTriage(TriageDecision("ask", None, "which service?", "fallback")))
    poller.assign([_assignment(imap.port, reply_enabled=False)])

    await poller.poll()

    [request] = stream.created
    assert request.outcome == "ask"
    assert FakeSmtp.sent == [], "with the toggle off the owner is asked, never the sender"


@pytest.mark.asyncio
async def test_ask_with_reply_on_records_one_reply_in_the_thread(imap: FakeImapServer) -> None:
    FakeSmtp.sent = []
    stream = RecordingStream()
    poller = _poller(stream, FixedTriage(TriageDecision("ask", None, "which service?", "fallback")))
    poller.assign([_assignment(imap.port, reply_enabled=True)])

    await poller.poll()

    [reply] = FakeSmtp.sent
    assert (reply["In-Reply-To"], reply["References"]) == ("<1@acme>", "<1@acme>")
    assert (reply["From"], reply["To"]) == (ME, ALICE)
    assert reply["Subject"] == "Re: the readiness page"
    assert "which service?" in reply.get_content()


# ----------------------------------------------------------------------------- slack


class FakeSlackWorkspace:
    """A workspace fixture: whether it lets members install apps, and its DMs."""

    def __init__(self, *, member_installs: bool) -> None:
        self.member_installs = member_installs
        self.posted: list[Mapping[str, str]] = []

    async def call(self, method: str, params: Mapping[str, str]) -> dict[str, Any]:
        if method == "auth.test":
            if not self.member_installs:
                return {"ok": False, "error": "restricted_action"}
            return {"ok": True, "user_id": "U0DANA"}
        if method == "conversations.list":
            return {"ok": True, "channels": [{"id": "D1"}]}
        if method == "conversations.history":
            return {"ok": True, "messages": [{"ts": "1.0", "user": "U0ALICE", "text": "help"}]}
        self.posted.append(params)
        return {"ok": True}


@pytest.mark.asyncio
async def test_the_slack_user_connector_refuses_where_member_installs_are_disabled() -> None:
    workspace = FakeSlackWorkspace(member_installs=False)
    assignment = _assignment(kind=UserSourceKind.SLACK_USER, address="T0ACME/U0DANA")

    with pytest.raises(SourceConnectRefusedError) as refused:
        await SlackUserConnector(assignment, workspace).connect()
    assert refused.value.code == MEMBER_INSTALLS_DISABLED

    triage = FixedTriage(TriageDecision("ignore", None, "chatter", "llm"))
    poller = UserSourcePoller(
        stream=RecordingStream(),
        store=FakeCredentialStore({REFERENCE: "xoxp-user-token"}),
        triage=triage,
        connector_factory=lambda a, _secret: SlackUserConnector(a, workspace),
    )
    poller.assign([assignment])
    await poller.poll()

    [status] = poller.statuses()
    assert (status.connected, status.error_code) == (False, MEMBER_INSTALLS_DISABLED)
    assert triage.seen == []


@pytest.mark.asyncio
async def test_the_slack_user_connector_reads_dms_and_replies_as_the_user() -> None:
    workspace = FakeSlackWorkspace(member_installs=True)
    assignment = _assignment(
        kind=UserSourceKind.SLACK_USER, address="T0ACME/U0DANA", reply_enabled=True
    ).model_copy(update={"intake_filter": SourceIntakeFilter()})
    stream = RecordingStream()
    poller = UserSourcePoller(
        stream=stream,
        store=FakeCredentialStore({REFERENCE: "xoxp-user-token"}),
        triage=FixedTriage(TriageDecision("ask", None, "which service?", "fallback")),
        connector_factory=lambda a, _secret: SlackUserConnector(a, workspace),
    )
    poller.assign([assignment])

    await poller.poll()
    await poller.poll()  # the same DM is not read twice

    [request] = stream.created
    assert request.requester == "U0ALICE"
    [posted] = workspace.posted
    assert (posted["channel"], posted["thread_ts"]) == ("D1", "1.0")
