"""How much of each subscription usage window a Contract has used (local-agents 10).

Asked of the vendor's own binary, as the Contract's uid in its harness root -- never of the
vendor's usage endpoint with the person's bearer (Anthropic's ``api/oauth/usage``,
OpenAI's ``wham/usage``), which would put that bearer in the Runner's hands. No adapter
spends model tokens. One adapter per harness behind one port (PRD decision 11):

- **Codex:** ``account/rateLimits/read`` over ``codex app-server``. codex-acp keeps rate
  limits in its session state and says nothing structured about them over ACP.
- **Claude Code:** the rate-limit events the CLI emits from its responses' headers, which
  claude-agent-acp (pinned 0.88.0, ``acp-agent.js``) forwards as a ``usage_update`` whose
  ``_meta["_claude/rateLimit"]`` is the CLI's ``rate_limit_info``. The ACP runtime reads
  them off the Directive's own stream, and a Contract whose stream has carried them is
  never probed. Until then, the ``/usage`` screen under a PTY, as Paperclip reads it
  (``claude-local/src/server/quota.ts``).

At most once per interval per Contract, never while it has a Directive running, and again
after a Directive that stopped on its usage limit. A probe that fails or times out reports
nothing until one succeeds; it never touches a Directive.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import logging
import math
import os
import pty
import re
import signal
import struct
import subprocess
import termios
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Final, Protocol

from agentic_runner.harness_self_test import DirectivesInFlight, SandboxFor
from agentic_runner.workers._runtime_support import terminate_process_tree
from agentic_runner.workers.claude_sign_in import launcher_argv
from agentic_runner.workers.contract_isolation import ContractIsolationError, DirectiveSandbox
from agentic_runner.workers.harness_outcome import next_wall_clock, wall_clock_zone
from agentic_runner_contracts.runner_registration import UsageWindow

__all__ = [
    "USAGE_PROBE_INTERVAL_SECONDS",
    "ClaudeUsageScreenProbe",
    "CodexRateLimitsProbe",
    "UsageWindowProbe",
    "UsageWindows",
    "claude_rate_limit_windows",
    "claude_usage_screen_windows",
    "codex_rate_limit_windows",
]

USAGE_PROBE_INTERVAL_SECONDS: Final = 15 * 60
_PROBE_TIMEOUT_SECONDS: Final = 20.0
_NAME: Final = re.compile(r"^[a-z][a-z0-9_]{0,31}$")

_logger = logging.getLogger(__name__)


class UsageWindowProbe(Protocol):
    """One harness's way of saying how much of each window is used, run in ``sandbox``."""

    async def __call__(self, sandbox: DirectiveSandbox) -> list[UsageWindow]: ...


class UsageWindows:
    """The latest windows per ``(contract_id, cli_kind)``, and when the next probe is due."""

    def __init__(
        self,
        *,
        probes: Mapping[str, UsageWindowProbe],
        sandbox_for: SandboxFor,
        in_flight: DirectivesInFlight,
        monotonic: Callable[[], float],
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        interval_seconds: float = USAGE_PROBE_INTERVAL_SECONDS,
    ) -> None:
        self._probes = dict(probes)
        self._sandbox_for = sandbox_for
        self._in_flight = in_flight
        self._monotonic = monotonic
        self._clock = clock
        self._interval = interval_seconds
        self._windows: dict[tuple[str, str], list[UsageWindow]] = {}
        self._last_run: dict[tuple[str, str], float] = {}
        self._streamed: set[tuple[str, str]] = set()

    def windows(self, contract_id: str, cli_kind: str) -> list[UsageWindow] | None:
        """``None`` until a window is known; a window whose reset has passed is dropped,
        since its percentage no longer describes anything."""

        now = self._clock()
        current = [
            window
            for window in self._windows.get((contract_id, cli_kind), [])
            if window.resets_at is None or window.resets_at > now
        ]
        return current or None

    def after_directive(
        self,
        contract_id: str,
        cli_kind: str,
        streamed: Sequence[UsageWindow],
        *,
        usage_limit: bool,
    ) -> None:
        """What a subscription Directive's own stream said, and whether issue 08 found it
        stopped on its usage limit -- which makes the next probe due at once."""

        key = (contract_id, cli_kind)
        if streamed:
            self._windows[key] = list(streamed)
            self._streamed.add(key)
            self._last_run[key] = self._monotonic()
        if usage_limit:
            self._last_run.pop(key, None)

    async def run_due(self, targets: Sequence[tuple[str, str]]) -> None:
        wanted = set(targets)
        for gone in (self._windows.keys() | self._last_run.keys() | self._streamed) - wanted:
            self._forget(gone)
        for contract_id, cli_kind in targets:
            key = (contract_id, cli_kind)
            probe = self._probes.get(cli_kind)
            if probe is None or key in self._streamed:
                continue
            last = self._last_run.get(key)
            if last is not None and self._monotonic() - last < self._interval:
                continue
            if self._in_flight.busy(contract_id):
                # Not stamped: it runs on the first sweep after the Directive ends.
                continue
            self._last_run[key] = self._monotonic()
            self._windows[key] = await self._probe(probe, contract_id, cli_kind)

    def _forget(self, key: tuple[str, str]) -> None:
        self._windows.pop(key, None)
        self._last_run.pop(key, None)
        self._streamed.discard(key)

    async def _probe(
        self, probe: UsageWindowProbe, contract_id: str, cli_kind: str
    ) -> list[UsageWindow]:
        try:
            sandbox = self._sandbox_for(contract_id, cli_kind)
            if sandbox is None:
                return []
            return await probe(sandbox)
        except (
            ContractIsolationError,
            OSError,
            TimeoutError,
            subprocess.SubprocessError,
            ValueError,
        ) as error:
            # The type only: a harness's error text can carry the account it signed in as.
            _logger.warning(
                "usage window probe for %s/%s failed: %s",
                contract_id,
                cli_kind,
                error.__class__.__name__,
            )
            return []


# ------------------------------------------------------------------ Codex


class CodexRateLimitsProbe:
    """``account/rateLimits/read`` over ``codex app-server``, as the Contract's uid.

    The app-server protocol is JSON-RPC without the ``jsonrpc`` member, one object per
    line, and interleaves notifications with the responses (codex-cli 0.159.2).
    """

    def __init__(
        self,
        *,
        argv: Sequence[str] = ("codex", "app-server"),
        timeout_seconds: float = _PROBE_TIMEOUT_SECONDS,
    ) -> None:
        self._argv = list(argv)
        self._timeout = timeout_seconds

    async def __call__(self, sandbox: DirectiveSandbox) -> list[UsageWindow]:
        process = await asyncio.create_subprocess_exec(
            *self._argv,
            cwd=sandbox.home_dir,
            env=_harness_env(sandbox, "CODEX_HOME"),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
            limit=1024 * 1024,
            **sandbox.spawn_kwargs(),
        )
        assert process.stdin is not None and process.stdout is not None
        stdin, stdout = process.stdin, process.stdout

        async def request(request_id: int, method: str, params: object) -> Mapping[str, Any]:
            stdin.write(_line({"id": request_id, "method": method, "params": params}))
            await stdin.drain()
            while line := await stdout.readline():
                with contextlib.suppress(ValueError):
                    message = json.loads(line)
                    if isinstance(message, dict) and message.get("id") == request_id:
                        return message
            raise ConnectionError("codex app-server closed its stdout")

        try:
            async with asyncio.timeout(self._timeout):
                await request(
                    1, "initialize", {"clientInfo": {"name": "agentic-runner", "version": "1"}}
                )
                stdin.write(_line({"method": "initialized"}))
                answer = await request(2, "account/rateLimits/read", None)
        finally:
            await terminate_process_tree(process)
        # An error -- ``-32600`` "codex account authentication required" when the root has
        # no login -- has no ``result``, and so no windows.
        return codex_rate_limit_windows(answer.get("result"))


_CODEX_WINDOW_NAMES: Final[Mapping[int, str]] = {300: "five_hour", 10_080: "seven_day"}


def codex_rate_limit_windows(result: object) -> list[UsageWindow]:
    """``rateLimits.primary``/``secondary`` from a ``GetAccountRateLimitsResponse``.

    Named by their duration where it is a window Codex is known to have, else by slot.
    ``usedPercent`` is already a percentage, ``resetsAt`` epoch seconds.
    """

    snapshot = result.get("rateLimits") if isinstance(result, Mapping) else None
    if not isinstance(snapshot, Mapping):
        return []
    windows: list[UsageWindow] = []
    for slot in ("primary", "secondary"):
        window = snapshot.get(slot)
        if not isinstance(window, Mapping):
            continue
        used = _number(window.get("usedPercent"))
        if used is None:
            continue
        minutes = window.get("windowDurationMins")
        name = _CODEX_WINDOW_NAMES.get(minutes, slot) if isinstance(minutes, int) else slot
        windows.append(
            UsageWindow(
                name=name, used_percent=_clamped(used), resets_at=_epoch(window.get("resetsAt"))
            )
        )
    return windows


# ------------------------------------------------------------------ Claude Code


def claude_rate_limit_windows(info: object) -> list[UsageWindow]:
    """The windows in one ``rate_limit_info`` (claude 2.1.295's ``rate_limit_event``).

    ``unifiedWindows`` carries every window on every event; without it, the top-level
    fields describe the one currently limiting. ``utilization`` is a fraction of the window
    and can run past 1.
    """

    if not isinstance(info, Mapping):
        return []
    unified = info.get("unifiedWindows")
    if isinstance(unified, Mapping) and unified:
        pairs = list(unified.items())
    else:
        pairs = [(info.get("rateLimitType"), info)]
    windows: list[UsageWindow] = []
    for name, window in pairs:
        if not isinstance(name, str) or not _NAME.match(name) or not isinstance(window, Mapping):
            continue
        utilization = _number(window.get("utilization"))
        if utilization is None:
            continue
        windows.append(
            UsageWindow(
                name=name,
                used_percent=_clamped(utilization * 100),
                resets_at=_epoch(window.get("resetsAt")),
            )
        )
    return windows


class ClaudeUsageScreenProbe:
    """``/usage`` typed into an interactive ``claude`` on a PTY, as the Contract's uid.

    Behind the sign-in launcher, so the CLI reads the Contract's own login and the Runner
    reads nothing under the harness root. Nothing is prompted: ``/usage`` is a local
    command and the session is killed once the screen has drawn, or at the timeout.
    """

    def __init__(
        self,
        *,
        argv: Sequence[str] = ("claude",),
        timeout_seconds: float = _PROBE_TIMEOUT_SECONDS,
        settle_seconds: float = 2.0,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._argv = list(argv)
        self._timeout = timeout_seconds
        self._settle = settle_seconds
        self._clock = clock

    async def __call__(self, sandbox: DirectiveSandbox) -> list[UsageWindow]:
        master_fd, slave_fd = pty.openpty()
        try:
            fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 200, 0, 0))
            os.set_blocking(master_fd, False)
            process = await asyncio.create_subprocess_exec(
                *launcher_argv(sandbox.harness_config_dir, self._argv),
                cwd=sandbox.home_dir,
                env=_harness_env(sandbox, "CLAUDE_CONFIG_DIR") | {"TERM": "xterm-256color"},
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                start_new_session=True,
                **sandbox.spawn_kwargs(),
            )
        except BaseException:
            os.close(master_fd)
            raise
        finally:
            os.close(slave_fd)
        screen = bytearray()
        windows: list[UsageWindow] = []
        try:
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(self._timeout):
                    # The prompt must be up before it can take a command.
                    await asyncio.sleep(self._settle)
                    _drain(master_fd, screen)
                    os.write(master_fd, b"/usage")
                    # Ink reads a command and its Enter as one paste and drops the Enter.
                    await asyncio.sleep(0.15)
                    os.write(master_fd, b"\r")
                    while not _screen_complete(windows):
                        await asyncio.sleep(0.25)
                        _drain(master_fd, screen)
                        windows = claude_usage_screen_windows(
                            screen.decode("utf-8", errors="replace"), self._clock()
                        )
        finally:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(process.pid, signal.SIGKILL)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(process.wait(), timeout=2)
            os.close(master_fd)
        return windows


_SCREEN_LABELS: Final[Mapping[str, str]] = {
    "currentsession": "five_hour",
    "currentweekallmodels": "seven_day",
    "currentweeksonnetonly": "seven_day_sonnet",
    "currentweeksonnet": "seven_day_sonnet",
    "currentweekopusonly": "seven_day_opus",
    "currentweekopus": "seven_day_opus",
}
_ANSI: Final = re.compile(
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC, e.g. a hyperlink
    r"|\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])"  # CSI and the two-byte escapes
)
_PERCENT: Final = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*%\s*(used|left|remaining|available)?", re.I)
# ``Resets 5pm (America/Chicago)``, ``Resets Mar 18 at 7:59am (America/Chicago)``; Ink can
# drop the spaces between the parts.
_RESET: Final = re.compile(
    r"^resets\s*(?:(?P<month>[a-z]{3})[a-z]*\s*(?P<day>\d{1,2}),?\s*(?:at\s*)?)?"
    r"(?P<clock>\d{1,2}(?::\d{2})?\s*[ap]\.?\s*m\.?)\s*(?:\((?P<zone>[^)]+)\))?",
    re.I,
)
_MONTHS: Final = (
    "jan",
    "feb",
    "mar",
    "apr",
    "may",
    "jun",
    "jul",
    "aug",
    "sep",
    "oct",
    "nov",
    "dec",
)


def claude_usage_screen_windows(text: str, now: datetime) -> list[UsageWindow]:
    """The windows on the latest ``/usage`` screen in a PTY capture.

    Each label is followed by its ``N% used`` line and its ``Resets …`` line. A window the
    screen draws without a percentage -- ``Extra usage``, or one still loading -- is left
    out, and so is a capture with no such screen at all.
    """

    lines = [line.strip() for line in _clean(text).split("\n")]
    starts = [i for i, line in enumerate(lines) if _label(line) == "currentsession"]
    if not starts:
        return []
    windows: dict[str, UsageWindow] = {}
    name: str | None = None
    used: float | None = None
    for line in lines[starts[-1] :]:
        label = _label(line)
        if label in _SCREEN_LABELS:
            name, used = _SCREEN_LABELS[label], None
        elif name is None or not line:
            continue
        elif used is None and (percent := _PERCENT.search(line)):
            value = float(percent.group(1))
            remaining = (percent.group(2) or "").lower() in {"left", "remaining", "available"}
            used = _clamped(100 - value if remaining else value)
            windows[name] = UsageWindow(name=name, used_percent=used)
        elif used is not None and (reset := _RESET.match(line)):
            windows[name] = UsageWindow(
                name=name, used_percent=used, resets_at=_screen_reset(reset, now)
            )
            name = None
    return list(windows.values())


def _screen_complete(windows: Sequence[UsageWindow]) -> bool:
    names = {window.name for window in windows}
    return "five_hour" in names and "seven_day" in names


def _screen_reset(match: re.Match[str], now: datetime) -> datetime | None:
    clock, zone = match.group("clock"), match.group("zone")
    if zone:
        clock = f"{clock} ({zone})"
    if match.group("month") is None:
        at = next_wall_clock(clock, now)
        return datetime.fromisoformat(at) if at else None
    month = match.group("month").lower()
    if month not in _MONTHS:
        return None
    # The time on the screen's date: the next such time after the date's midnight.
    local_now = now.astimezone(wall_clock_zone(zone))
    try:
        midnight = local_now.replace(
            month=_MONTHS.index(month) + 1,
            day=int(match.group("day")),
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
    except ValueError:
        return None
    if midnight.date() < local_now.date():
        # A reset is never in the past: ``Jan 2`` seen on Dec 30 is next year's.
        midnight = midnight.replace(year=midnight.year + 1)
    at = next_wall_clock(clock, midnight)
    return datetime.fromisoformat(at) if at else None


def _clean(text: str) -> str:
    kept: list[str] = []
    for char in text:
        if char == "\b":
            if kept:
                kept.pop()
        else:
            kept.append(char)
    return _ANSI.sub("", "".join(kept)).replace("\x00", "").replace("\r", "\n")


def _label(line: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", line.lower())


def _drain(master_fd: int, screen: bytearray) -> None:
    with contextlib.suppress(BlockingIOError, OSError):
        while chunk := os.read(master_fd, 65_536):
            screen.extend(chunk)
            # The latest screen is at the end; a CLI that redraws forever is bounded here.
            del screen[:-262_144]


# ------------------------------------------------------------------ shared


def _harness_env(sandbox: DirectiveSandbox, root_env: str) -> dict[str, str]:
    """The four names every probe gets and nothing else: no bearer, no API key, no proxy."""

    return {
        "PATH": os.environ.get("PATH") or os.defpath,
        "HOME": str(sandbox.home_dir),
        "TMPDIR": str(sandbox.tmp_dir),
        root_env: str(sandbox.harness_config_dir),
    }


def _line(message: Mapping[str, object]) -> bytes:
    return (json.dumps(message) + "\n").encode()


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) else None


def _clamped(percent: float) -> float:
    return round(min(100.0, max(0.0, percent)), 1)


def _epoch(value: object) -> datetime | None:
    seconds = _number(value)
    if seconds is None or seconds <= 0:
        return None
    try:
        return datetime.fromtimestamp(seconds, UTC)
    except (OverflowError, OSError, ValueError):
        return None
