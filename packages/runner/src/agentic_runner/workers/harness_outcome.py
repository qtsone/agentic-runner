"""Recognise a subscription harness that stopped on its person's account (local-agents 08).

A usage limit or an expired sign-in is not something a fix Directive can repair, so the
Runner names it on the Directive's output (``HarnessHold``) and issue 09 holds instead of
failing. Pure text parsing, no I/O, re-implemented from the behaviour Paperclip's
``claude-local`` and ``codex-local`` adapters classify (research 01 §1, at ``6bc830b``) and
from the ACP spike's captures (local-agents 02: ``xLIM``, ``xEXP``, ``cEXP``).

A miss costs what it cost before this module -- a generic failure and a fix Directive --
while a false match holds a Contract that could have run. So every matcher is narrow: it
reads a failed turn only, and only the harness's structured error fields and stderr,
never the model's own messages, which may well talk about usage limits or sign-ins.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any, Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from agentic_runner.workers.harness_usage import extract_claude_result
from agentic_runner_contracts.activity_io import HarnessHold, HarnessHoldKind

__all__ = ["classify_harness_outcome"]

_CLAUDE_CODE: Final[str] = "claude_code"
_CODEX_CLI: Final[str] = "codex_cli"

# The ACP runtime raises a bridge's JSON-RPC error as ``ACP error <code>: <message>
# <data as sorted JSON>`` into ``DirectiveResult.error`` (local-agents 12). ``-32000`` is
# ACP's "authentication required", which both bridges answer for an expired or absent
# sign-in (``xEXP``, ``cEXP``); codex-acp marks a usage limit as ``-32603`` with
# ``codexErrorInfo: usageLimitExceeded`` and no reset time (``xLIM``).
_ACP_ERROR_RE: Final = re.compile(r"ACP error (-?\d+):")
_ACP_AUTH_REQUIRED: Final[int] = -32000
_ACP_INTERNAL_ERROR: Final[int] = -32603
_CODEX_USAGE_LIMIT_INFO_RE: Final = re.compile(r'"codexErrorInfo":\s*"usageLimitExceeded"')

_APOSTROPHE: Final[str] = "['’]"

_CODEX_USAGE_LIMIT_RE: Final = re.compile(
    rf"you{_APOSTROPHE}ve hit your usage limit|usage limit (?:reached|exceeded)",
    re.IGNORECASE,
)
_CODEX_SIGN_IN_RE: Final = re.compile(
    r"refresh[_\s-]?token[_\s-]?(?:reused|expired|invalidated|revoked)"
    r"|refresh token (?:has )?(?:already been used|expired|been invalidated|been revoked)"
    r"|\binvalid_grant\b"
    r"|(?:oauth|refresh|access[_\s-]?token|bearer).{0,80}(?:\b401\b|unauthori[sz]ed)"
    r"|(?:\b401\b|unauthori[sz]ed).{0,80}(?:oauth|refresh|access[_\s-]?token|bearer)",
    re.IGNORECASE,
)
_CODEX_RESET_RE: Final = re.compile(r"try again at\s+(?P<clock>[^.!\n]+)", re.IGNORECASE)

_CLAUDE_USAGE_LIMIT_RE: Final = re.compile(
    rf"you{_APOSTROPHE}ve hit your (?:\w+ )?limit"
    rf"|you{_APOSTROPHE}re out of extra usage"
    r"|session limit (?:reached|exceeded)"
    r"|(?:5[-\s]?hour|weekly|claude usage|usage) limit reached",
    re.IGNORECASE,
)
_CLAUDE_SIGN_IN_RE: Final = re.compile(
    r"not logged in|please log in|please run (?:`?claude login`?|/login)"
    r"|login required|authentication required"
    r"|invalid api key.{0,120}(?:/login|claude login|log in)"
    r"|authentication[_\s-](?:failed|error)|failed to authenticate"
    r"|invalid bearer token"
    r"|(?:invalid|expired|revoked).{0,40}(?:bearer|oauth|access) token"
    r"|(?:bearer|oauth|access) token.{0,40}(?:invalid|expired|revoked)",
    re.IGNORECASE,
)
_CLAUDE_RESET_RE: Final = re.compile(r"\bresets?\s+(?:at\s+)?(?P<clock>[^\n]+)", re.IGNORECASE)

# ``4pm``, ``11:31 PM``, ``3:15 AM (UTC)``, ``4:30 PM (America/Chicago)``.
_CLOCK_RE: Final = re.compile(
    r"^(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<half>[ap])\.?\s*m\.?"
    r"(?:\s*\((?P<zone>[^)]+)\))?",
    re.IGNORECASE,
)


def classify_harness_outcome(
    cli_kind: str,
    *,
    exit_code: int,
    stdout: str,
    stderr: str,
    error: str = "",
    now: datetime,
) -> HarnessHold | None:
    """``usage_limit`` (with ``retry_not_before`` when the harness named a reset),
    ``sign_in_required``, or None for every other outcome, a successful turn included.

    ``now`` is when the turn ended, timezone-aware: a reset is printed as a wall-clock
    time, so it is the next such time after ``now`` -- in the zone the harness named, or
    the Runner host's own, where the harness that printed it ran.
    """

    if exit_code == 0:
        return None
    acp_hold = _acp_hold(error)
    if acp_hold is not None:
        return acp_hold
    if cli_kind == _CLAUDE_CODE:
        text = _joined(error, stderr, *_claude_error_fields(stdout))
        usage_limit, sign_in, reset = _CLAUDE_USAGE_LIMIT_RE, _CLAUDE_SIGN_IN_RE, _CLAUDE_RESET_RE
    elif cli_kind == _CODEX_CLI:
        text = _joined(error, stderr, *_codex_error_events(stdout))
        usage_limit, sign_in, reset = _CODEX_USAGE_LIMIT_RE, _CODEX_SIGN_IN_RE, _CODEX_RESET_RE
    else:
        return None
    # A sign-in is checked first: a harness that cannot authenticate never reached the
    # account whose limit it might also mention.
    if sign_in.search(text):
        return HarnessHold(kind=HarnessHoldKind.SIGN_IN_REQUIRED)
    limit = usage_limit.search(text)
    if limit is None:
        return None
    clock = reset.search(text, limit.end())
    return HarnessHold(
        kind=HarnessHoldKind.USAGE_LIMIT,
        retry_not_before=_next_wall_clock(clock.group("clock"), now) if clock else None,
    )


def _acp_hold(error: str) -> HarnessHold | None:
    match = _ACP_ERROR_RE.search(error)
    if match is None:
        return None
    code = int(match.group(1))
    if code == _ACP_AUTH_REQUIRED:
        return HarnessHold(kind=HarnessHoldKind.SIGN_IN_REQUIRED)
    if code == _ACP_INTERNAL_ERROR and _CODEX_USAGE_LIMIT_INFO_RE.search(error):
        return HarnessHold(kind=HarnessHoldKind.USAGE_LIMIT)
    return None


def _codex_error_events(stdout: str) -> list[str]:
    """The messages of ``codex exec --json``'s ``error`` and ``turn.failed`` events."""

    messages: list[str] = []
    for event in _json_lines(stdout):
        if event.get("type") == "error":
            messages.append(str(event.get("message") or ""))
        elif event.get("type") == "turn.failed":
            failure = event.get("error")
            if isinstance(failure, dict):
                messages.append(str(failure.get("message") or ""))
    return messages


def _claude_error_fields(stdout: str) -> list[str]:
    """A failed ``result`` message's text, ``error`` and ``errors`` -- never an assistant
    message, which is the model talking."""

    result = extract_claude_result(stdout)
    if result is None or not result.get("is_error"):
        return []
    fields = [str(result.get("result") or ""), str(result.get("error") or "")]
    errors = result.get("errors")
    if isinstance(errors, list):
        for entry in errors:
            if isinstance(entry, dict):
                fields.append(str(entry.get("message") or entry.get("error") or ""))
            else:
                fields.append(str(entry))
    return fields


def _json_lines(stdout: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _joined(*parts: str) -> str:
    return "\n".join(part.strip() for part in parts if part and part.strip())


def _next_wall_clock(text: str, now: datetime) -> str | None:
    match = _CLOCK_RE.match(text.strip())
    if match is None:
        return None
    hour, minute = int(match.group("hour")), int(match.group("minute") or 0)
    if not 1 <= hour <= 12 or not 0 <= minute <= 59:
        return None
    hour = hour % 12 + (12 if match.group("half").lower() == "p" else 0)
    local_now = now.astimezone(_zone(match.group("zone")))
    candidate = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= local_now:
        candidate += timedelta(days=1)
    return candidate.astimezone(UTC).isoformat()


def _zone(name: str | None) -> tzinfo | None:
    """The zone the harness named, or None -- ``astimezone``'s "this host's own"."""

    if not name:
        return None
    if name.strip().upper() in {"UTC", "GMT"}:
        return UTC
    try:
        return ZoneInfo(name.strip())
    except (ZoneInfoNotFoundError, ValueError):
        return None
