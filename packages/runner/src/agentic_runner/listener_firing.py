"""Listeners on a member's own connected Slack, run on the Runner that holds its token
(Spec: chat intake rev 2, QTS-2392).

On the Organisation's Slack the control plane matches each event against its Listeners
and runs every firing's harness tools with the token it holds. On a member's own Slack
the token is the member's and never leaves this host, so the same parts run here:

1. **Scan** each conversation a Listener hears for what is new since the last poll: top
   level messages, replies in threads, and reactions (:class:`SlackScanner`).
2. **Match** every event against every Listener without an LLM (:func:`listener_matches`),
   so a non-match costs nothing.
3. **Fire** each matching Listener on its own (:class:`ListenerFirer`): an automatic one
   opens work with its filled instructions and spends no tokens deciding; any other runs
   the firing Directive through this Runner's LLM proxy, funded on its Agent's Contract
   and stopped at its token cap, with only the tools the Listener enables
   (:class:`SlackListenerTools`). The Agent never holds the token: the tools call Slack
   for it.

What reaches the control plane is what reaches it from the Triage Directive today -- a
Work Record request, an ids-only ignore report -- plus a Draft for the member's Inbox.
"""

from __future__ import annotations

import functools
import json
import logging
import re
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Final, Literal, Protocol
from uuid import UUID, uuid4

import re2  # type: ignore[import-untyped]

from agentic_runner_contracts.listeners import (
    CALLABLE_TOOLS,
    DRAFT_TOOL,
    MESSAGE_MAX_CHARS,
    OPEN_WORK_TOOL,
    REACT_TOOL,
    READ_MAX_MESSAGES,
    READ_TOOL,
    REPLY_MAX_CHARS,
    REPLY_TOOL,
    UNTRUSTED_MAX_CHARS,
    ListenerAssignment,
    ListenerDraftRequest,
    build_firing_prompt,
    build_read_follow_up,
    firing_directive_id,
    reads,
    render_instructions,
    tool_calls,
)
from agentic_runner_contracts.triage import TriageCandidate, decide
from agentic_runner_contracts.user_sources import (
    IntakeIgnoredReport,
    IntakeWorkRequest,
    IntakeWorkResponse,
    UserSourceAssignment,
    message_ref,
)

__all__ = [
    "ListenerFirer",
    "ListenerIntake",
    "ListenerTurn",
    "SlackEvent",
    "SlackListenerTools",
    "SlackScanner",
    "listener_matches",
]

_logger = logging.getLogger(__name__)

EventKind = Literal["channel_message", "direct_message", "reaction"]

# How far back each poll looks in a conversation. A reaction or a thread reply lands on a
# message already seen, which ``oldest`` would hide, so every poll re-reads this window.
SCAN_WINDOW: Final = 50
# Slack caps a message at 40k characters; a pattern only ever sees this much of it.
_PATTERN_INPUT_MAX_LENGTH: Final = 4000
_DESCRIPTION_MAX: Final = 4000
_EMOJI_NAME: Final = re.compile(r"[a-z0-9_+'-]{1,64}")
# Slack's special mentions (`<!channel>`, `<!here>`, ...) all open with `<!`. Escaped,
# Slack shows the text and notifies nobody.
_SPECIAL_MENTION: Final = "<!"
_READ_ELSEWHERE: Final = "this firing read another channel, so it may not {}"
_HOURLY: Final = 3600.0
_RE2_OPTIONS: Final = re2.Options()
_RE2_OPTIONS.log_errors = False


class SlackApi(Protocol):
    """Slack's Web API under the member's user token (``user_sources.HttpSlackUserApi``)."""

    async def call(self, method: str, params: Mapping[str, str]) -> dict[str, Any]: ...


class ListenerIntake(Protocol):
    """The signed calls a firing makes to the control plane."""

    async def create_work_record(self, request: IntakeWorkRequest) -> IntakeWorkResponse: ...

    async def report_ignored(self, report: IntakeIgnoredReport) -> None: ...

    async def create_draft(self, request: ListenerDraftRequest) -> None: ...


class ListenerTurn(Protocol):
    """One completion of the firing Directive through this Runner's LLM proxy; ``None`` is
    a turn that cannot be trusted, which the keyword fallback answers."""

    async def __call__(
        self, listener: ListenerAssignment, *, directive_id: str, prompt: str
    ) -> str | None: ...


@dataclass(frozen=True)
class SlackEvent:
    """What one Slack event says, as matching reads it. ``thread_ts == message_ts`` on a
    top-level message. On a reaction the message is the one reacted to, the sender is who
    reacted, and ``text`` is empty."""

    kind: EventKind
    channel_id: str
    sender_id: str
    text: str
    message_ts: str
    thread_ts: str
    is_bot: bool = False
    # Posted by the member whose Slack this is -- the identity a firing posts as too.
    is_own_message: bool = False
    # The member is @mentioned: on their own Slack the member is who a mention addresses.
    is_mention: bool = False
    # A reply in a thread the member started, or one this Listener's Agent answered in.
    in_my_thread: bool = False
    reaction: str | None = None

    @property
    def message_id(self) -> str:
        return f"{self.channel_id}:{self.message_ts}"


def listener_matches(
    listener: ListenerAssignment,
    event: SlackEvent,
    *,
    member_ids: frozenset[str],
    answered_threads: frozenset[tuple[str, str]] = frozenset(),
) -> bool:
    """Whether ``event`` fires ``listener``: from the event alone, never an LLM."""

    match = listener.match
    if event.is_bot and match.exclude_bots:
        return False
    if event.is_own_message and match.exclude_own_messages:
        return False
    if event.kind == "direct_message":
        if match.signal != "direct_message":
            return False
    elif event.channel_id not in match.channel_ids:
        return False
    if match.who == "members" and event.sender_id not in member_ids:
        return False
    if match.who == "listed" and event.sender_id not in match.sender_ids:
        return False
    if (event.kind == "reaction") != (match.signal == "reaction"):
        return False
    if match.signal == "reaction":
        assert event.reaction is not None
        # A skin-toned reaction arrives as ``thumbsup::skin-tone-2``; it is still a thumbsup.
        return event.reaction.split("::", 1)[0] == match.emoji
    if match.signal in ("direct_message", "every_message"):
        return True
    if match.signal == "mention":
        return event.is_mention
    if match.signal == "thread_reply":
        return event.thread_ts != event.message_ts and (
            event.in_my_thread or (event.channel_id, event.thread_ts) in answered_threads
        )
    if not match.pattern:
        return False
    return _compiled(match.pattern).search(event.text[:_PATTERN_INPUT_MAX_LENGTH]) is not None


@functools.lru_cache(maxsize=256)
def _compiled(pattern: str) -> Any:
    # Never Python's ``re``: any Slack sender controls the input, and a backtracking engine
    # lets one message pin this Runner. The control plane compiled it with RE2 when it was
    # written; one that does not compile here never matches.
    try:
        return re2.compile(pattern, options=_RE2_OPTIONS)
    except re2.error:
        return _NEVER


class _Never:
    def search(self, _text: str) -> None:
        return None


_NEVER: Final = _Never()


# ----------------------------------------------------------------------------- scan


@dataclass
class _Cursor:
    latest_ts: Decimal
    reactions: frozenset[tuple[str, str, str]]
    threads: dict[str, str]


@dataclass
class SlackScanner:
    """What is new in the member's conversations since the last poll.

    The first scan of a conversation only marks where it stands: a Listener added to a
    busy channel, or a Runner restarted, would otherwise fire on the whole window and
    spend a turn on every message in it. A restart therefore misses what arrived while
    the Runner was down, as a webhook missed while the control plane is would be.
    """

    api: SlackApi
    me: str
    _cursors: dict[str, _Cursor] = field(default_factory=dict)
    # What a firing posted, as the member: never an event, whatever a Listener excludes,
    # or one firing's reply would wake the next.
    _posted: deque[str] = field(default_factory=lambda: deque(maxlen=500))

    def posted(self, channel_id: str, ts: str) -> None:
        self._posted.append(f"{channel_id}:{ts}")

    async def scan(self, channel_ids: Sequence[str], *, direct_messages: bool) -> list[SlackEvent]:
        conversations = [(channel_id, False) for channel_id in channel_ids]
        if direct_messages:
            listed = await self.api.call("conversations.list", {"types": "im"})
            conversations += [
                (str(channel.get("id")), True) for channel in listed.get("channels") or []
            ]
        events: list[SlackEvent] = []
        for channel_id, is_im in conversations:
            events += await self._scan_one(channel_id, is_im=is_im)
        live = {channel_id for channel_id, _ in conversations}
        for channel_id in list(self._cursors):
            if channel_id not in live:
                del self._cursors[channel_id]
        return sorted(events, key=lambda event: _ts(event.message_ts))

    async def _scan_one(self, channel_id: str, *, is_im: bool) -> list[SlackEvent]:
        history = await self.api.call(
            "conversations.history", {"channel": channel_id, "limit": str(SCAN_WINDOW)}
        )
        items = [item for item in reversed(history.get("messages") or []) if item.get("ts")]
        cursor = self._cursors.get(channel_id)
        kind: EventKind = "direct_message" if is_im else "channel_message"
        events: list[SlackEvent] = []
        reactions: set[tuple[str, str, str]] = set()
        threads: dict[str, str] = {}
        latest = cursor.latest_ts if cursor is not None else Decimal(0)
        for item in items:
            ts = str(item["ts"])
            if cursor is not None and _ts(ts) > cursor.latest_ts:
                events.append(self._message_event(item, channel_id, kind, ts, ts))
            latest = max(latest, _ts(ts))
            for reaction in item.get("reactions") or []:
                name = str(reaction.get("name") or "")
                for user in reaction.get("users") or []:
                    key = (ts, name, str(user))
                    reactions.add(key)
                    if cursor is not None and key not in cursor.reactions:
                        events.append(
                            SlackEvent(
                                kind="reaction",
                                channel_id=channel_id,
                                sender_id=str(user),
                                text="",
                                message_ts=ts,
                                thread_ts=str(item.get("thread_ts") or ts),
                                is_own_message=str(user) == self.me,
                                reaction=name,
                            )
                        )
            latest_reply = item.get("latest_reply")
            if item.get("reply_count") and latest_reply:
                threads[ts] = str(latest_reply)
                if cursor is not None and cursor.threads.get(ts) != latest_reply:
                    events += await self._replies(
                        item, channel_id, kind, since=cursor.threads.get(ts, ts)
                    )
        self._cursors[channel_id] = _Cursor(
            latest_ts=latest, reactions=frozenset(reactions), threads=threads
        )
        return [event for event in events if event.message_id not in self._posted]

    async def _replies(
        self, parent: Mapping[str, Any], channel_id: str, kind: EventKind, *, since: str
    ) -> list[SlackEvent]:
        thread_ts = str(parent["ts"])
        found = await self.api.call(
            "conversations.replies",
            {"channel": channel_id, "ts": thread_ts, "oldest": since, "limit": str(SCAN_WINDOW)},
        )
        return [
            self._message_event(
                item,
                channel_id,
                kind,
                str(item["ts"]),
                thread_ts,
                in_my_thread=str(parent.get("user") or "") == self.me,
            )
            for item in found.get("messages") or []
            if item.get("ts") and _ts(str(item["ts"])) > _ts(since) and item["ts"] != thread_ts
        ]

    def _message_event(
        self,
        item: Mapping[str, Any],
        channel_id: str,
        kind: EventKind,
        ts: str,
        thread_ts: str,
        *,
        in_my_thread: bool = False,
    ) -> SlackEvent:
        sender = str(item.get("user") or item.get("bot_id") or "")
        text = str(item.get("text") or "")
        return SlackEvent(
            kind=kind,
            channel_id=channel_id,
            sender_id=sender,
            text=text,
            message_ts=ts,
            thread_ts=str(item.get("thread_ts") or thread_ts),
            is_bot=bool(item.get("bot_id")) or item.get("subtype") == "bot_message",
            is_own_message=sender == self.me,
            is_mention=f"<@{self.me}>" in text,
            in_my_thread=in_my_thread or str(item.get("parent_user_id") or "") == self.me,
        )


def _ts(value: str) -> Decimal:
    # Slack's ts is a fixed-point string; a float would round two messages into one.
    try:
        return Decimal(value)
    except InvalidOperation:
        return Decimal(0)


# ----------------------------------------------------------------------------- tools


@dataclass(frozen=True)
class ToolResult:
    """What one call did. ``messages`` is a Read's result; it is untrusted input."""

    tool: str
    ok: bool
    detail: str
    messages: list[dict[str, object]] | None = None

    def as_json(self) -> dict[str, object]:
        result: dict[str, object] = {"tool": self.tool, "ok": self.ok, "detail": self.detail}
        if self.messages is not None:
            result["messages"] = self.messages
        return result


class SlackListenerTools:
    """The harness Tool Server of one firing, on the Runner that holds the member's token.

    It lists the Listener's enabled tools and nothing else; a call to any other is refused,
    whatever the message asked for. Reply and React read the Source's reply switch from the
    latest heartbeat ack on every call (``reply_enabled``), so switching replies off in the
    console silences a firing already under way. Read keeps working with replies off and
    refuses a channel outside the Listener's read scope; Draft keeps working too, because a
    draft reaches nobody until the member sends it.

    Once a firing has read a channel other than its own, its Reply and Draft are refused
    for the rest of the firing (QTS-2453): a message in one channel could otherwise have
    the Agent read another and post what it found to everyone in the first.
    """

    def __init__(
        self,
        *,
        source_id: UUID,
        listener: ListenerAssignment,
        event: SlackEvent,
        api: SlackApi,
        intake: ListenerIntake,
        reply_enabled: Callable[[], bool],
        on_posted: Callable[[str, str], None] = lambda _channel, _ts: None,
    ) -> None:
        self._source_id = source_id
        self._listener = listener
        self._event = event
        self._api = api
        self._intake = intake
        self._reply_enabled = reply_enabled
        self._on_posted = on_posted
        self._read_elsewhere = False

    @property
    def tools(self) -> tuple[str, ...]:
        return tuple(tool for tool in CALLABLE_TOOLS if tool in self._listener.tools)

    @property
    def opens_work(self) -> bool:
        return OPEN_WORK_TOOL in self._listener.tools

    @property
    def may_reply(self) -> bool:
        """Whether a Reply could go out now."""

        return REPLY_TOOL in self.tools and not self._read_elsewhere and self._reply_enabled()

    async def call(self, call: Mapping[str, object]) -> ToolResult:
        tool = str(call.get("tool"))
        if tool not in self.tools:
            return ToolResult(tool=tool, ok=False, detail="no such tool")
        if tool == READ_TOOL:
            return await self._read(call)
        if tool == REPLY_TOOL:
            return await self.reply(call.get("text"))
        if tool == DRAFT_TOOL:
            return await self._draft(call)
        return await self._react(call)

    async def reply(self, text: object) -> ToolResult:
        if not isinstance(text, str) or not text.strip():
            return ToolResult(tool=REPLY_TOOL, ok=False, detail="reply needs text")
        if self._read_elsewhere:
            return ToolResult(tool=REPLY_TOOL, ok=False, detail=_READ_ELSEWHERE.format("reply"))
        if not self._reply_enabled():
            return ToolResult(tool=REPLY_TOOL, ok=False, detail="replies are off on this Source")
        answer = await self._api.call(
            "chat.postMessage",
            {
                "channel": self._event.channel_id,
                "text": text[:REPLY_MAX_CHARS].replace(_SPECIAL_MENTION, "&lt;!"),
                "thread_ts": self._event.thread_ts,
            },
        )
        if not answer.get("ok"):
            return ToolResult(tool=REPLY_TOOL, ok=False, detail=str(answer.get("error")))
        self._on_posted(self._event.channel_id, str(answer.get("ts") or ""))
        return ToolResult(tool=REPLY_TOOL, ok=True, detail="posted")

    async def context(self, sender_name: str | None = None) -> dict[str, object]:
        """What the Listener hands its Agent before it asks: the triggering event always,
        then whatever its context options ask for, each within its limit. The oldest of
        what was read gives way first when the whole is over its size limit."""

        options = self._listener.context
        event = self._event
        context: dict[str, object] = {
            "message": {
                "channel_id": event.channel_id,
                "ts": event.message_ts,
                "thread_ts": event.thread_ts,
                "sender_id": event.sender_id,
                "sender_name": sender_name,
                "text": event.text[:MESSAGE_MAX_CHARS],
            }
        }
        if event.reaction is not None:
            context["reaction"] = event.reaction
        if options.thread and event.thread_ts != event.message_ts:
            thread = await self._read_or_none(
                event.channel_id, event.thread_ts, options.thread_max_messages
            )
            if thread is not None:
                context["thread"] = thread
        if options.channel_last_messages:
            channel = await self._read_or_none(
                event.channel_id, None, options.channel_last_messages
            )
            if channel is not None:
                context["channel"] = channel
        if options.sender_profile:
            answer = await self._api.call("users.info", {"user": event.sender_id})
            user = answer.get("user") if answer.get("ok") else None
            if isinstance(user, dict):
                context["sender"] = {
                    "id": str(user.get("id") or event.sender_id),
                    "name": user.get("name"),
                    "real_name": user.get("real_name"),
                    "is_bot": bool(user.get("is_bot")),
                }
        for key in ("channel", "thread"):
            messages = context.get(key)
            while isinstance(messages, list) and messages and _size(context) > UNTRUSTED_MAX_CHARS:
                messages.pop(0)
        return context

    async def _draft(self, call: Mapping[str, object]) -> ToolResult:
        text = call.get("text")
        if not isinstance(text, str) or not text.strip():
            return ToolResult(tool=DRAFT_TOOL, ok=False, detail="draft needs text")
        if self._read_elsewhere:
            return ToolResult(tool=DRAFT_TOOL, ok=False, detail=_READ_ELSEWHERE.format("draft"))
        await self._intake.create_draft(
            ListenerDraftRequest(
                source_id=self._source_id,
                listener_id=self._listener.listener_id,
                channel_id=self._event.channel_id,
                thread_ts=self._event.thread_ts,
                body=text[:REPLY_MAX_CHARS],
            )
        )
        return ToolResult(tool=DRAFT_TOOL, ok=True, detail="drafted for a person to check")

    async def _react(self, call: Mapping[str, object]) -> ToolResult:
        emoji = call.get("emoji")
        name = emoji.strip(":") if isinstance(emoji, str) else ""
        if not _EMOJI_NAME.fullmatch(name):
            return ToolResult(tool=REACT_TOOL, ok=False, detail="react needs an emoji name")
        if not self._reply_enabled():
            return ToolResult(tool=REACT_TOOL, ok=False, detail="replies are off on this Source")
        answer = await self._api.call(
            "reactions.add",
            {"channel": self._event.channel_id, "timestamp": self._event.message_ts, "name": name},
        )
        if not answer.get("ok"):
            return ToolResult(tool=REACT_TOOL, ok=False, detail=str(answer.get("error")))
        return ToolResult(tool=REACT_TOOL, ok=True, detail="added")

    async def _read(self, call: Mapping[str, object]) -> ToolResult:
        channel_id = call.get("channel_id")
        thread_ts = call.get("thread_ts")
        limit = call.get("limit", READ_MAX_MESSAGES)
        if (
            not isinstance(channel_id, str)
            or not channel_id
            or not (thread_ts is None or isinstance(thread_ts, str))
            or not isinstance(limit, int)
            or isinstance(limit, bool)
        ):
            return ToolResult(tool=READ_TOOL, ok=False, detail="read needs a channel_id")
        if not self._in_read_scope(channel_id):
            return ToolResult(
                tool=READ_TOOL,
                ok=False,
                detail=f"{channel_id} is outside this Listener's read scope",
            )
        try:
            messages = await self._fetch(
                channel_id, thread_ts or None, max(1, min(limit, READ_MAX_MESSAGES))
            )
        except SlackReadError as error:
            return ToolResult(tool=READ_TOOL, ok=False, detail=str(error))
        if channel_id != self._event.channel_id:
            self._read_elsewhere = True
        return ToolResult(tool=READ_TOOL, ok=True, detail="read", messages=messages)

    def _in_read_scope(self, channel_id: str) -> bool:
        listener = self._listener
        return (
            listener.read_scope == "all_channels"
            or channel_id == self._event.channel_id
            or channel_id in listener.match.channel_ids
        )

    async def _fetch(
        self, channel_id: str, thread_ts: str | None, limit: int
    ) -> list[dict[str, object]]:
        params = {"channel": channel_id, "limit": str(limit)}
        if thread_ts is None:
            answer = await self._api.call("conversations.history", params)
        else:
            answer = await self._api.call("conversations.replies", {**params, "ts": thread_ts})
        if not answer.get("ok"):
            raise SlackReadError(str(answer.get("error")))
        found = list(answer.get("messages") or [])
        if thread_ts is None:
            # History is newest first; a thread already reads oldest first.
            found.reverse()
        return [
            {
                "ts": str(item.get("ts") or ""),
                "user": str(item.get("user") or item.get("bot_id") or ""),
                "text": str(item.get("text") or "")[:MESSAGE_MAX_CHARS],
            }
            for item in found[-limit:]
        ]

    async def _read_or_none(
        self, channel_id: str, thread_ts: str | None, limit: int
    ) -> list[dict[str, object]] | None:
        try:
            return await self._fetch(channel_id, thread_ts, limit)
        except SlackReadError as error:
            # Context is a head start, never a precondition: the Agent can still answer, or
            # read for itself if the Listener lets it.
            _logger.warning("Listener context: read of %s unavailable: %s", channel_id, error)
            return None


class SlackReadError(RuntimeError):
    """Slack refused a read; the message is Slack's error code."""


def _within_limit(results: list[dict[str, object]]) -> list[dict[str, object]]:
    while _size(results) > UNTRUSTED_MAX_CHARS:
        largest = max(
            (result.get("messages") for result in results),
            key=lambda messages: len(messages) if isinstance(messages, list) else 0,
        )
        if not isinstance(largest, list) or not largest:
            break
        largest.pop(0)
    return results


def _size(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False))


# ----------------------------------------------------------------------------- firing


@dataclass
class HourlyCap:
    """A Listener's firing cap per hour. Over it the event is skipped and counted; the
    member's Notification is the control plane's, once the count reaches it."""

    clock: Callable[[], float] = time.monotonic
    _fired: dict[UUID, deque[float]] = field(default_factory=dict)
    skipped: dict[UUID, int] = field(default_factory=dict)

    def admit(self, listener: ListenerAssignment) -> bool:
        now = self.clock()
        fired = self._fired.setdefault(listener.listener_id, deque())
        while fired and now - fired[0] >= _HOURLY:
            fired.popleft()
        if len(fired) >= listener.hourly_cap:
            self.skipped[listener.listener_id] = self.skipped.get(listener.listener_id, 0) + 1
            return False
        fired.append(now)
        return True


@dataclass
class ListenerFirer:
    """Every event of one poll against every Listener of one Source: each match fires on
    its own, and one firing that fails never stops the others."""

    intake: ListenerIntake
    turn: ListenerTurn
    cap: HourlyCap = field(default_factory=HourlyCap)
    # Threads a firing's Agent posted in: "a thread the Agent started or answered in".
    _answered: dict[UUID, set[tuple[str, str]]] = field(default_factory=dict)

    async def fire_all(
        self,
        assignment: UserSourceAssignment,
        events: Sequence[SlackEvent],
        *,
        api: SlackApi,
        scanner: SlackScanner,
        reply_enabled: Callable[[], bool],
    ) -> None:
        members = frozenset(assignment.member_ids)
        for event in events:
            for listener in assignment.listeners:
                answered = frozenset(self._answered.get(listener.agent_id, set()))
                if not listener_matches(
                    listener, event, member_ids=members, answered_threads=answered
                ):
                    continue
                if not self.cap.admit(listener):
                    _logger.warning(
                        "Listener %s is over its hourly cap; event skipped", listener.listener_id
                    )
                    continue
                tools = SlackListenerTools(
                    source_id=assignment.source_id,
                    listener=listener,
                    event=event,
                    api=api,
                    intake=self.intake,
                    reply_enabled=reply_enabled,
                    on_posted=scanner.posted,
                )
                try:
                    await self.fire(assignment, listener, event, tools)
                except Exception as error:  # noqa: BLE001 - one firing never stops the others
                    _logger.warning(
                        "Listener %s firing failed: %s", listener.listener_id, type(error).__name__
                    )

    async def fire(
        self,
        assignment: UserSourceAssignment,
        listener: ListenerAssignment,
        event: SlackEvent,
        tools: SlackListenerTools,
    ) -> None:
        instructions = render_instructions(
            listener.instructions_template,
            {
                "product.name": assignment.product_name,
                "channel.name": f"<#{event.channel_id}>",
                "sender.is_member": "yes" if event.sender_id in assignment.member_ids else "no",
                # The Listener's own emoji, not the event's: a sender picks the skin tone.
                "reaction.emoji": listener.match.emoji or "",
            },
        )
        directive_id = firing_directive_id(uuid4())
        refs = [message_ref(event.message_id)]
        if listener.mode == "automatic":
            await self._open_work_automatically(
                assignment, listener, event, instructions, directive_id, refs
            )
            return
        candidates = [
            TriageCandidate(agent_id=c.agent_id, specialisation=c.specialisation)
            for c in assignment.candidates
        ]
        prompt = build_firing_prompt(
            instructions=instructions or None,
            tools=tools.tools,
            opens_work=tools.opens_work,
            candidates=candidates,
            context=await tools.context(),
        )
        raw = await self.turn(listener, directive_id=directive_id, prompt=prompt)
        calls = reads(tool_calls(raw)) if READ_TOOL in tools.tools else []
        if raw is not None and calls:
            # An answer that reads gets one more turn with what it read, and only one:
            # every turn may spend up to the firing's token cap. A read the Listener does
            # not enable buys no turn at all.
            results = _within_limit([(await tools.call(call)).as_json() for call in calls])
            prompt = build_read_follow_up(prompt, raw_text=raw, results=results)
            raw = await self.turn(listener, directive_id=directive_id, prompt=prompt)
        decision = decide(raw, event.text, candidates=candidates)
        ran: list[ToolResult] = []
        if decision.source == "llm":
            # The fallback classifier's decision is the harness's, not the Agent's: an
            # answer that could not be trusted calls nothing.
            ran = [
                await tools.call(call) for call in tool_calls(raw) if call.get("tool") != READ_TOOL
            ]
        replied = any(result.ok and result.tool == REPLY_TOOL for result in ran)
        if replied:
            self._answered.setdefault(listener.agent_id, set()).add(
                (event.channel_id, event.thread_ts)
            )
        if decision.outcome == "ignore" or not tools.opens_work:
            await self.intake.report_ignored(
                IntakeIgnoredReport(
                    source_id=assignment.source_id,
                    product_id=assignment.product_id,
                    directive_id=directive_id,
                    classification_source=decision.source,
                    message_refs=refs,
                    listener_id=listener.listener_id,
                )
            )
            return
        await self.intake.create_work_record(
            IntakeWorkRequest(
                source_id=assignment.source_id,
                product_id=assignment.product_id,
                directive_id=directive_id,
                outcome=decision.outcome,
                classification_source=decision.source,
                reason=decision.reason[:500],
                lead_agent_id=decision.lead_agent_id,
                description=_description(event),
                requester=event.sender_id or assignment.address,
                budget_max_tokens=listener.token_cap,
                message_refs=refs,
                listener_id=listener.listener_id,
            )
        )
        if not replied and tools.may_reply:
            text = (
                f"Taken -- routed to a Lead. {decision.reason}"
                if decision.outcome == "own"
                else f"I need a hand with this: {decision.reason}"
            )
            if (await tools.reply(text)).ok:
                self._answered.setdefault(listener.agent_id, set()).add(
                    (event.channel_id, event.thread_ts)
                )

    async def _open_work_automatically(
        self,
        assignment: UserSourceAssignment,
        listener: ListenerAssignment,
        event: SlackEvent,
        instructions: str,
        directive_id: str,
        refs: list[str],
    ) -> None:
        """An automatic Listener (Spec: chat intake rev 2, "Mode"): a Work Record led by the
        Listener's Agent with the filled instructions as its description. No Directive
        decides, so no Agent tokens are spent deciding."""

        description = instructions
        if event.text:
            description += f"\n\nThe message (untrusted input):\n{event.text}"
        await self.intake.create_work_record(
            IntakeWorkRequest(
                source_id=assignment.source_id,
                product_id=assignment.product_id,
                directive_id=directive_id,
                outcome="own",
                classification_source="automatic",
                reason="An automatic Listener opened this work.",
                lead_agent_id=listener.agent_id,
                description=description.strip()[:_DESCRIPTION_MAX] or "(empty message)",
                requester=event.sender_id or assignment.address,
                message_refs=refs,
                listener_id=listener.listener_id,
            )
        )


def _description(event: SlackEvent) -> str:
    text = event.text.strip() or (
        f"(a :{event.reaction}: reaction)" if event.reaction else "(empty message)"
    )
    return f"From: {event.sender_id}\n{text}"[:_DESCRIPTION_MAX]
