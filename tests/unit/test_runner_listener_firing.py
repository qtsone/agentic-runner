"""Listeners on a member's own connected Slack (Spec: chat intake rev 2, QTS-2392).

Matching is pinned as a pure function, per signal and per who-option. The firing is pinned
end to end through the real poller: a fake Slack workspace answers the member's own token,
a recording turn stands in for the LLM proxy, and a recording stream for the signed calls
to the control plane. What is pinned is what the Runner sends -- which Listener fires,
which Slack calls go out under the member's token, and what reaches the control plane.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from typing import Any
from uuid import UUID, uuid4

import pytest

from agentic_runner.credentials import FakeCredentialStore
from agentic_runner.listener_firing import (
    HourlyCap,
    SlackEvent,
    SlackScanner,
    listener_matches,
)
from agentic_runner.user_sources import SlackUserConnector, UserSourcePoller
from agentic_runner_contracts.listeners import (
    ListenerAssignment,
    ListenerDraftRequest,
    ListenerMatchSpec,
)
from agentic_runner_contracts.triage import TriageDecision
from agentic_runner_contracts.user_sources import (
    IntakeIgnoredReport,
    IntakeWorkRequest,
    IntakeWorkResponse,
    UserSourceAssignment,
    UserSourceKind,
)

ME = "U0DANA"
ALICE = "U0ALICE"
BUGS = "C0BUGS"
RANDOM = "C0RANDOM"
REFERENCE = "acme-slack"


def _listener(
    *,
    signal: str = "every_message",
    channels: tuple[str, ...] = (BUGS,),
    tools: tuple[str, ...] = ("open_work", "reply"),
    **match: Any,
) -> ListenerAssignment:
    return ListenerAssignment(
        listener_id=uuid4(),
        agent_id=uuid4(),
        contract_id=uuid4(),
        match=ListenerMatchSpec.model_validate(
            {
                "signal": signal,
                "channel_ids": [] if signal == "direct_message" else list(channels),
                **match,
            }
        ),
        instructions_template="Triage {{message.text}} for {{product.name}}",
        tools=list(tools),
    )


def _event(text: str = "the build is red", **fields: Any) -> SlackEvent:
    values: dict[str, Any] = {
        "kind": "channel_message",
        "channel_id": BUGS,
        "sender_id": ALICE,
        "text": text,
        "message_ts": "100.000001",
        "thread_ts": "100.000001",
        **fields,
    }
    return SlackEvent(**values)


def _matches(listener: ListenerAssignment, event: SlackEvent, **kwargs: Any) -> bool:
    return listener_matches(listener, event, member_ids=frozenset({ALICE}), **kwargs)


# ----------------------------------------------------------------------------- matching


def test_each_signal_matches_only_its_own_events() -> None:
    assert _matches(_listener(signal="every_message"), _event())
    assert not _matches(_listener(signal="every_message"), _event(channel_id=RANDOM))

    mention = _listener(signal="mention")
    assert _matches(mention, _event(f"<@{ME}> look", is_mention=True))
    assert not _matches(mention, _event("look"))

    dm = _listener(signal="direct_message")
    assert _matches(dm, _event(kind="direct_message", channel_id="D1"))
    assert not _matches(dm, _event())
    assert not _matches(_listener(), _event(kind="direct_message", channel_id="D1"))

    pattern = _listener(signal="pattern", pattern=r"(?i)\bbuild\b.*red")
    assert _matches(pattern, _event())
    assert not _matches(pattern, _event("all green"))

    reply = _listener(signal="thread_reply")
    in_thread = _event(message_ts="101.0", thread_ts="100.000001")
    assert not _matches(reply, in_thread)
    assert _matches(reply, _event(message_ts="101.0", in_my_thread=True))
    assert _matches(reply, in_thread, answered_threads=frozenset({(BUGS, "100.000001")}))
    assert not _matches(reply, _event(in_my_thread=True))  # a top-level message is no reply


def test_a_reaction_matches_its_emoji_whatever_the_skin_tone() -> None:
    bug = _listener(signal="reaction", emoji="thumbsup")
    reacted = _event("", kind="reaction", reaction="thumbsup::skin-tone-2")
    assert _matches(bug, reacted)
    assert not _matches(bug, _event("", kind="reaction", reaction="eyes"))
    assert not _matches(bug, _event())
    assert not _matches(_listener(), reacted)


def test_who_and_the_default_exclusions() -> None:
    assert not _matches(_listener(who="listed", sender_ids=["U0BOB"]), _event())
    assert _matches(_listener(who="listed", sender_ids=[ALICE]), _event())
    assert _matches(_listener(who="members"), _event())
    assert not _matches(_listener(who="members"), _event(sender_id="U0STRANGER"))

    assert not _matches(_listener(), _event(is_bot=True))
    assert not _matches(_listener(), _event(sender_id=ME, is_own_message=True))
    assert _matches(_listener(exclude_bots=False), _event(is_bot=True))


def test_a_pattern_matches_in_linear_time() -> None:
    # Catastrophic under a backtracking engine: Python's `re` takes minutes on this input.
    listener = _listener(signal="pattern", pattern=r"(a+)+$")
    started = time.monotonic()
    assert not _matches(listener, _event("a" * 3000 + "!"))
    assert time.monotonic() - started < 1.0


# ----------------------------------------------------------------------------- fakes


class FakeSlack:
    """The member's workspace under their own token: channels, DMs, threads, reactions."""

    def __init__(self) -> None:
        self.channels: dict[str, list[dict[str, Any]]] = {BUGS: [], RANDOM: [], "D1": []}
        self.replies: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.failing: set[str] = set()
        self._clock = 100

    def say(self, channel: str, text: str, *, user: str = ALICE) -> str:
        self._clock += 1
        ts = f"{self._clock}.000001"
        self.channels[channel].append({"ts": ts, "user": user, "text": text})
        return ts

    def answer(self, channel: str, parent_ts: str, text: str, *, user: str = ALICE) -> None:
        self._clock += 1
        ts = f"{self._clock}.000001"
        parent = next(item for item in self.channels[channel] if item["ts"] == parent_ts)
        parent["reply_count"] = parent.get("reply_count", 0) + 1
        parent["latest_reply"] = ts
        self.replies.setdefault((channel, parent_ts), []).append(
            {"ts": ts, "user": user, "text": text, "thread_ts": parent_ts}
        )

    def react(self, channel: str, ts: str, name: str, *, user: str = ALICE) -> None:
        item = next(item for item in self.channels[channel] if item["ts"] == ts)
        item.setdefault("reactions", []).append({"name": name, "users": [user]})

    def sent(self, method: str) -> list[dict[str, str]]:
        return [params for called, params in self.calls if called == method]

    async def call(self, method: str, params: Mapping[str, str]) -> dict[str, Any]:
        self.calls.append((method, dict(params)))
        if method in self.failing:
            return {"ok": False, "error": "ratelimited"}
        if method == "auth.test":
            return {"ok": True, "user_id": ME}
        if method == "conversations.list":
            return {"ok": True, "channels": [{"id": "D1"}]}
        if method == "conversations.history":
            newest_first = list(reversed(self.channels[params["channel"]]))
            return {"ok": True, "messages": newest_first[: int(params.get("limit", "100"))]}
        if method == "conversations.replies":
            parent = next(
                item for item in self.channels[params["channel"]] if item["ts"] == params["ts"]
            )
            thread = self.replies.get((params["channel"], params["ts"]), [])
            return {"ok": True, "messages": [parent, *thread]}
        if method == "chat.postMessage":
            ts = self.say(params["channel"], params["text"], user=ME)
            return {"ok": True, "ts": ts}
        if method == "reactions.add":
            return {"ok": True}
        if method == "users.info":
            return {"ok": True, "user": {"id": params["user"], "name": "alice"}}
        raise AssertionError(f"unexpected Slack call {method}")


class RecordingIntake:
    def __init__(self) -> None:
        self.created: list[IntakeWorkRequest] = []
        self.ignored: list[IntakeIgnoredReport] = []
        self.drafts: list[ListenerDraftRequest] = []

    async def create_work_record(self, request: IntakeWorkRequest) -> IntakeWorkResponse:
        self.created.append(request)
        return IntakeWorkResponse(work_record_id=uuid4())

    async def report_ignored(self, report: IntakeIgnoredReport) -> None:
        self.ignored.append(report)

    async def create_draft(self, request: ListenerDraftRequest) -> None:
        self.drafts.append(request)


class RecordingTurn:
    """The firing Directive's turn: answers in order, and keeps every prompt it was given."""

    def __init__(self, *answers: dict[str, Any]) -> None:
        self.answers = [json.dumps(answer) for answer in answers]
        self.turns: list[tuple[ListenerAssignment, str, str]] = []

    async def __call__(
        self, listener: ListenerAssignment, *, directive_id: str, prompt: str
    ) -> str | None:
        self.turns.append((listener, directive_id, prompt))
        return self.answers.pop(0) if self.answers else None


class NeverTriage:
    async def __call__(self, *_: object) -> tuple[TriageDecision, str]:
        raise AssertionError("a Source with Listeners runs no Triage Directive")


def _source(
    *listeners: ListenerAssignment, reply_enabled: bool = True, lead: UUID | None = None
) -> UserSourceAssignment:
    return UserSourceAssignment(
        source_id=UUID(int=1),
        product_id=UUID(int=2),
        kind=UserSourceKind.SLACK_USER,
        address=f"T0ACME/{ME}",
        credential_reference=REFERENCE,
        reply_enabled=reply_enabled,
        intake_lead_agent_id=UUID(int=3),
        intake_lead_contract_id=UUID(int=4),
        candidates=[{"agent_id": lead or uuid4(), "specialisation": "development"}],
        listeners=list(listeners),
        product_name="Checkout",
        member_ids=[ALICE],
    )


async def _poller(
    slack: FakeSlack, intake: RecordingIntake, turn: RecordingTurn, *sources: UserSourceAssignment
) -> UserSourcePoller:
    poller = UserSourcePoller(
        stream=intake,
        store=FakeCredentialStore({REFERENCE: "xoxp-member-token"}),
        triage=NeverTriage(),
        connector_factory=lambda assignment, _secret: SlackUserConnector(assignment, slack),
        listener_turn=turn,
    )
    poller.assign(list(sources))
    await poller.poll()  # the first scan only marks where each conversation stands
    return poller


def _answer(outcome: str = "ignore", *calls: dict[str, Any], lead: UUID | None = None) -> Any:
    return {
        "outcome": outcome,
        "lead_agent_id": None if lead is None else str(lead),
        "reason": "seen",
        "tool_calls": list(calls),
    }


# ----------------------------------------------------------------------------- firing


@pytest.mark.asyncio
async def test_every_matching_listener_fires_on_its_own_and_a_non_match_spends_nothing() -> None:
    slack, intake = FakeSlack(), RecordingIntake()
    first, second = _listener(), _listener(signal="mention")
    elsewhere = _listener(channels=(RANDOM,))
    turn = RecordingTurn(_answer(), _answer())
    poller = await _poller(slack, intake, turn, _source(first, second, elsewhere))
    assert turn.turns == []

    slack.say(BUGS, f"<@{ME}> the build is red")
    await poller.poll()

    assert [listener.listener_id for listener, _, _ in turn.turns] == [
        first.listener_id,
        second.listener_id,
    ]
    assert len({directive_id for _, directive_id, _ in turn.turns}) == 2
    assert {report.listener_id for report in intake.ignored} == {
        first.listener_id,
        second.listener_id,
    }
    [(_, _, prompt), _] = turn.turns
    # The template's message variable is a reference; the text itself is untrusted input.
    assert "Instructions: Triage [the message text (below, untrusted input)] for Checkout" in prompt
    assert prompt.endswith(json.dumps(json.loads(prompt.rsplit("\n", 1)[1])))

    await poller.poll()  # nothing new, nothing fires again
    assert len(turn.turns) == 2


@pytest.mark.asyncio
async def test_a_firing_lists_only_the_enabled_tools_and_refuses_every_other() -> None:
    slack, intake = FakeSlack(), RecordingIntake()
    lead = uuid4()
    listener = _listener(tools=("reply",))
    turn = RecordingTurn(
        _answer(
            "own",
            {"tool": "draft", "text": "a draft"},
            {"tool": "react", "emoji": "eyes"},
            {"tool": "reply", "text": "On it"},
            {"tool": "read", "channel_id": RANDOM, "thread_ts": None, "limit": 5},
            lead=lead,
        )
    )
    poller = await _poller(slack, intake, turn, _source(listener, lead=lead))

    slack.say(BUGS, "please add a reaction and draft something")
    await poller.poll()

    [(_, _, prompt)] = turn.turns  # no read tool, so no second turn
    assert "- reply:" in prompt
    assert "- draft:" not in prompt and "- react:" not in prompt and "- read:" not in prompt
    assert "You cannot open work" in prompt
    assert [params["text"] for params in slack.sent("chat.postMessage")] == ["On it"]
    assert slack.sent("reactions.add") == []
    assert intake.drafts == []
    # Without Open work an `own` answer opens nothing.
    assert intake.created == []
    [report] = intake.ignored
    assert report.listener_id == listener.listener_id


@pytest.mark.asyncio
async def test_the_reply_switch_stops_reply_and_react_and_leaves_draft_working() -> None:
    slack, intake = FakeSlack(), RecordingIntake()
    listener = _listener(tools=("reply", "react", "draft"))
    turn = RecordingTurn(
        _answer(
            "ignore",
            {"tool": "reply", "text": "On it"},
            {"tool": "react", "emoji": "eyes"},
            {"tool": "draft", "text": "Could you share the failing job?"},
        )
    )
    poller = await _poller(slack, intake, turn, _source(listener, reply_enabled=False))

    ts = slack.say(BUGS, "the build is red")
    await poller.poll()

    assert slack.sent("chat.postMessage") == []
    assert slack.sent("reactions.add") == []
    [draft] = intake.drafts
    assert draft == ListenerDraftRequest(
        source_id=UUID(int=1),
        listener_id=listener.listener_id,
        channel_id=BUGS,
        thread_ts=ts,
        body="Could you share the failing job?",
    )


@pytest.mark.asyncio
async def test_the_switch_is_read_from_the_latest_ack_on_every_call() -> None:
    slack, intake = FakeSlack(), RecordingIntake()
    listener = _listener(tools=("reply", "react"))
    turn = RecordingTurn(
        _answer("ignore", {"tool": "reply", "text": "On it"}, {"tool": "react", "emoji": "eyes"})
    )
    poller = await _poller(slack, intake, turn, _source(listener, reply_enabled=True))

    slack.say(BUGS, "the build is red")
    poller.assign([_source(listener, reply_enabled=False)])
    await poller.poll()

    assert slack.sent("chat.postMessage") == [] and slack.sent("reactions.add") == []


@pytest.mark.asyncio
async def test_open_work_names_the_listener_and_the_outcome_replies_in_the_thread() -> None:
    slack, intake = FakeSlack(), RecordingIntake()
    lead = uuid4()
    listener = _listener(tools=("open_work", "reply"))
    turn = RecordingTurn(_answer("own", lead=lead))
    poller = await _poller(slack, intake, turn, _source(listener, lead=lead))

    ts = slack.say(BUGS, "the build is red")
    await poller.poll()

    [request] = intake.created
    assert (request.outcome, request.lead_agent_id, request.listener_id) == (
        "own",
        lead,
        listener.listener_id,
    )
    assert request.requester == ALICE
    [posted] = slack.sent("chat.postMessage")
    assert (posted["channel"], posted["thread_ts"]) == (BUGS, ts)
    assert posted["text"].startswith("Taken")

    # Its own reply is no event: a firing's post never wakes the next firing.
    await poller.poll()
    assert len(turn.turns) == 1


@pytest.mark.asyncio
async def test_an_automatic_listener_opens_work_without_a_turn() -> None:
    slack, intake = FakeSlack(), RecordingIntake()
    listener = _listener(tools=()).model_copy(update={"mode": "automatic"})
    turn = RecordingTurn()
    poller = await _poller(slack, intake, turn, _source(listener))

    slack.say(BUGS, "the build is red")
    await poller.poll()

    assert turn.turns == []
    [request] = intake.created
    assert (request.classification_source, request.lead_agent_id) == (
        "automatic",
        listener.agent_id,
    )
    assert request.description.startswith(
        "Triage [the message text (below, untrusted input)] for Checkout"
    )
    assert request.description.endswith("The message (untrusted input):\nthe build is red")


@pytest.mark.asyncio
async def test_a_read_gets_one_more_turn_and_a_read_elsewhere_silences_reply() -> None:
    slack, intake = FakeSlack(), RecordingIntake()
    listener = _listener(tools=("reply", "read", "draft")).model_copy(
        update={"read_scope": "all_channels"}
    )
    slack.say(RANDOM, "the deploy key rotated")
    turn = RecordingTurn(
        _answer("ignore", {"tool": "read", "channel_id": RANDOM, "thread_ts": None, "limit": 5}),
        _answer(
            "ignore", {"tool": "reply", "text": "the key rotated"}, {"tool": "draft", "text": "x"}
        ),
    )
    poller = await _poller(slack, intake, turn, _source(listener))

    slack.say(BUGS, "why is the build red?")
    await poller.poll()

    [_, (_, _, follow_up)] = turn.turns
    assert "the deploy key rotated" in follow_up
    assert slack.sent("chat.postMessage") == [] and intake.drafts == []


@pytest.mark.asyncio
async def test_a_read_outside_the_scope_is_refused() -> None:
    slack, intake = FakeSlack(), RecordingIntake()
    listener = _listener(tools=("read",))
    slack.say(RANDOM, "the deploy key rotated")
    turn = RecordingTurn(
        _answer("ignore", {"tool": "read", "channel_id": RANDOM, "thread_ts": None, "limit": 5}),
        _answer(),
    )
    poller = await _poller(slack, intake, turn, _source(listener))

    slack.say(BUGS, "why is the build red?")
    await poller.poll()

    [_, (_, _, follow_up)] = turn.turns
    assert "outside this Listener's read scope" in follow_up
    assert "the deploy key rotated" not in follow_up


@pytest.mark.asyncio
async def test_thread_replies_reactions_and_direct_messages_are_events() -> None:
    slack, intake = FakeSlack(), RecordingIntake()
    mine = slack.say(BUGS, "is the build red?", user=ME)
    theirs = slack.say(BUGS, "the build is red")
    replies = _listener(signal="thread_reply")
    reactions = _listener(signal="reaction", emoji="bug")
    dms = _listener(signal="direct_message")
    turn = RecordingTurn(_answer(), _answer(), _answer())
    poller = await _poller(slack, intake, turn, _source(replies, reactions, dms))

    slack.answer(BUGS, mine, "yes, since noon")
    slack.answer(BUGS, theirs, "a reply in someone else's thread")
    slack.react(BUGS, theirs, "bug")
    slack.say("D1", "can you look at the build?")
    await poller.poll()

    fired = {listener.listener_id for listener, _, _ in turn.turns}
    assert fired == {replies.listener_id, reactions.listener_id, dms.listener_id}
    assert len(turn.turns) == 3


@pytest.mark.asyncio
async def test_a_failed_read_keeps_the_cursor_so_recovery_fires_on_nothing_old() -> None:
    slack = FakeSlack()
    old = slack.say(BUGS, "the build is red")
    slack.react(BUGS, old, "bug")
    slack.answer(BUGS, old, "since noon")
    slack.say("D1", "an old direct message")
    scanner = SlackScanner(api=slack, me=ME)
    assert await scanner.scan([BUGS], direct_messages=True) == []

    slack.failing = {"conversations.history", "conversations.list"}
    assert await scanner.scan([BUGS], direct_messages=True) == []
    slack.failing = set()
    assert await scanner.scan([BUGS], direct_messages=True) == []

    new = slack.say("D1", "a new direct message")
    events = await scanner.scan([BUGS], direct_messages=True)
    assert [(event.channel_id, event.message_ts) for event in events] == [("D1", new)]


@pytest.mark.asyncio
async def test_a_first_scan_that_fails_leaves_the_next_one_to_baseline() -> None:
    slack = FakeSlack()
    old = slack.say(BUGS, "the build is red")
    slack.react(BUGS, old, "bug")
    scanner = SlackScanner(api=slack, me=ME)
    slack.failing = {"conversations.history"}
    assert await scanner.scan([BUGS], direct_messages=False) == []
    slack.failing = set()
    assert await scanner.scan([BUGS], direct_messages=False) == []

    new = slack.say(BUGS, "still red")
    events = await scanner.scan([BUGS], direct_messages=False)
    assert [event.message_ts for event in events] == [new]


@pytest.mark.asyncio
async def test_a_failed_replies_read_is_read_again_and_loses_no_reply() -> None:
    slack = FakeSlack()
    parent = slack.say(BUGS, "the build is red")
    scanner = SlackScanner(api=slack, me=ME)
    await scanner.scan([BUGS], direct_messages=False)

    slack.answer(BUGS, parent, "since noon")
    slack.failing = {"conversations.replies"}
    assert await scanner.scan([BUGS], direct_messages=False) == []
    slack.failing = set()
    events = await scanner.scan([BUGS], direct_messages=False)
    assert [event.text for event in events] == ["since noon"]
    assert await scanner.scan([BUGS], direct_messages=False) == []


@pytest.mark.asyncio
async def test_over_the_hourly_cap_the_event_is_skipped_and_counted() -> None:
    slack, intake = FakeSlack(), RecordingIntake()
    listener = _listener().model_copy(update={"hourly_cap": 1})
    turn = RecordingTurn(_answer(), _answer())
    poller = await _poller(slack, intake, turn, _source(listener))

    slack.say(BUGS, "one")
    slack.say(BUGS, "two")
    await poller.poll()

    assert len(turn.turns) == 1
    assert poller._firer is not None
    assert poller._firer.cap.skipped == {listener.listener_id: 1}


def test_the_hourly_cap_admits_again_after_an_hour() -> None:
    now = [0.0]
    cap = HourlyCap(clock=lambda: now[0])
    listener = _listener().model_copy(update={"hourly_cap": 1})
    assert cap.admit(listener)
    assert not cap.admit(listener)
    now[0] = 3600.0
    assert cap.admit(listener)


# ----------------------------------------------------------------------------- the wire


def test_a_request_without_a_listener_reads_as_before_on_an_older_control_plane() -> None:
    # The intake requests forbid extra keys, so a control plane on contracts 3.32 refuses
    # `"listener_id": null`. The Runner sends them without their unset fields.
    request = IntakeWorkRequest(
        source_id=uuid4(),
        product_id=uuid4(),
        directive_id="triage:x",
        outcome="ask",
        classification_source="llm",
        reason="which service?",
        description="help",
        requester=ALICE,
        message_refs=["a" * 32],
    )
    sent = json.loads(request.model_dump_json(exclude_none=True))
    assert "listener_id" not in sent
    assert IntakeWorkRequest.model_validate(sent) == request

    # And a 3.32 ack, which names no Listener, still parses.
    legacy = _source().model_dump(mode="json")
    for key in ("listeners", "product_name", "member_ids"):
        legacy.pop(key)
    assert UserSourceAssignment.model_validate(legacy).listeners == []
