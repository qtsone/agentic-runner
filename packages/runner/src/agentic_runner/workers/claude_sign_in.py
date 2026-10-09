"""Claude Code's in-place sign-in for one Contract (local-agents 05, PRD decision 13).

Two methods, both made in the Contract's own harness root and neither ever delivered:

* ``oauth_token`` (the default): ``claude setup-token`` mints a long-lived (1-year)
  subscription token. The CLI prints it and exits; this module writes it to
  ``{harness_root}/oauth-token`` (0600, the Contract's uid) and drops it. It is the
  only moment the Runner process holds it (PRD decision 13 amends decision 1 for this).
* ``claude_ai``: ``claude auth login --claudeai``, a refreshing login the CLI keeps itself
  -- ``.credentials.json`` on Linux, a Keychain item on macOS.

Both are interactive paste-code flows with no device code (claude 2.1.295: S256 PKCE, the
CLI waits at "Paste code here if prompted >"), so the CLI runs on a pseudo-terminal and
the code the browser shows the person comes back sealed on the heartbeat ack
(:meth:`ClaudeSignIns.relay`). The PKCE verifier never leaves the CLI's process, so the
code alone signs nobody in.

A waiting sign-in is the one thing here held in the Runner's process: the PTY is the only
way to reach the CLI. A restart closes it and the CLI exits; the person starts again.

macOS (the 2026-10-08 spike): Claude saves a ``claude_ai`` login with the ``security`` CLI
into the default keychain of the ``HOME`` it is given, and a Contract's sandbox ``HOME``
has none -- the save fails. So that method gets a per-Contract keychain in the harness
root, made the sandbox ``HOME``'s default, and the launcher unlocks it before every spawn:
a locked keychain makes ``security`` block on a GUI prompt that nobody will answer.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import logging
import os
import pty
import re
import secrets
import signal
import struct
import sys
import termios
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

from agentic_runner.workers._runtime_support import run_subprocess_exec
from agentic_runner.workers.contract_isolation import ContractIsolation, DirectiveSandbox
from agentic_runner_contracts.sealed_credential import SignInCodeOutcome

__all__ = [
    "CLAUDE_AI",
    "CLAUDE_SIGN_IN_ARGV",
    "KEYCHAIN_FILE",
    "KEYCHAIN_PASSWORD_FILE",
    "OAUTH_TOKEN",
    "OAUTH_TOKEN_FILE",
    "ClaudeAuthStatus",
    "ClaudeSignInError",
    "ClaudeSignInPrompt",
    "ClaudeSignIns",
    "UnknownSignInMethodError",
    "launcher_argv",
]

RUNTIME_KIND: Final[str] = "claude_code"
OAUTH_TOKEN: Final[str] = "oauth_token"
CLAUDE_AI: Final[str] = "claude_ai"

CLAUDE_SIGN_IN_ARGV: Final[dict[str, tuple[str, ...]]] = {
    OAUTH_TOKEN: ("claude", "setup-token"),
    CLAUDE_AI: ("claude", "auth", "login", "--claudeai"),
}
_STATUS_ARGV: Final[tuple[str, ...]] = ("claude", "auth", "status", "--json")

OAUTH_TOKEN_FILE: Final[str] = "oauth-token"
KEYCHAIN_FILE: Final[str] = "claude.keychain-db"
KEYCHAIN_PASSWORD_FILE: Final[str] = "keychain-password"

# Runs as the Contract's uid, so the token and the keychain password are read by the
# Contract's own process and never by the Runner's. The scrub is claude's own switch: it
# strips CLAUDE_CODE_OAUTH_TOKEN (and every other Anthropic credential) from the env of
# every command the Agent runs, so `env` in a Directive does not print the token.
_LAUNCHER: Final[str] = """\
root=$1
shift
if [ -f "$root/oauth-token" ]; then
  CLAUDE_CODE_OAUTH_TOKEN=$(cat -- "$root/oauth-token") || exit 126
  export CLAUDE_CODE_OAUTH_TOKEN
fi
if [ -f "$root/keychain-password" ]; then
  security unlock-keychain "$root/claude.keychain-db" <"$root/keychain-password" \\
    >/dev/null 2>&1 || exit 126
fi
CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1
export CLAUDE_CODE_SUBPROCESS_ENV_SCRUB
exec "$@"
"""

# A relayed code expires after this, or when the waiting CLI exits, whichever is first;
# the CLI's process group is killed at the same moment.
SIGN_IN_WINDOW: Final[timedelta] = timedelta(minutes=10)
# Paperclip's setup-token-runner.ts:44-60: Ink reads a paste and its Enter as one chunk
# and drops the Enter, so the newline goes in a separate write a beat later.
_ENTER_DELAY_SECONDS: Final[float] = 0.15
# Ink wraps the authorize URL and the token at the terminal width; one line each keeps
# them whole.
_PTY_COLUMNS: Final[int] = 1000
_PTY_ROWS: Final[int] = 50
_DEFAULT_PROMPT_TIMEOUT_SECONDS: Final[float] = 30.0
_STATUS_TIMEOUT_SECONDS: Final[int] = 30
_OUTPUT_LIMIT_BYTES: Final[int] = 65_536

_logger = logging.getLogger(__name__)

# Runs as the Contract's uid, never the Runner's: the Contract owns every directory down to
# its harness root and can swap one for a symlink while a sign-in waits. The Runner, with
# CAP_CHOWN and CAP_DAC_OVERRIDE, would follow it into another Contract's root; as the
# Contract's uid the write or unlink reaches only what that Contract already could.
# ``value`` None removes the file.
_PRIVATE_FILE_SCRIPT: Final[str] = """
import json, os, secrets, sys
path, value = json.load(sys.stdin)
if value is None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    sys.exit()
directory, name = os.path.split(path)
temporary = os.path.join(directory, "." + name + "." + secrets.token_hex(8) + ".tmp")
flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW
try:
    with os.fdopen(os.open(temporary, flags, 0o600), "w", encoding="utf-8") as handle:
        handle.write(value)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
except BaseException:
    try:
        os.unlink(temporary)
    except FileNotFoundError:
        pass
    raise
"""

# The URL arrives as an OSC 8 hyperlink whose target is whole even when its visible text
# is split; the plain form is the fallback for a CLI that prints it bare.
_OSC8_URL: Final[re.Pattern[str]] = re.compile(r"\x1b\]8;[^;\x07]*;(https://[^\x07\x1b]+)")
_PLAIN_URL: Final[re.Pattern[str]] = re.compile(r"https://\S+/oauth/authorize\?[^\s\x07\x1b]+")
_ANSI: Final[re.Pattern[str]] = re.compile(r"\x1b(?:\][^\x07]*\x07|\[[0-9;?>]*[A-Za-z]|[78])")
_TOKEN: Final[re.Pattern[str]] = re.compile(r"sk-ant-oat\d\d-[A-Za-z0-9_-]{20,}")
# What a browser shows the person to paste back: base64url, with `#` joining the state.
_CODE_SHAPE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_\-#.~]{8,512}$")


class ClaudeSignInError(RuntimeError):
    """The CLI never printed a usable sign-in prompt, or its status could not be read."""


class UnknownSignInMethodError(ValueError):
    """A ``method`` this Runner has no Claude Code sign-in command for."""


@dataclass(frozen=True, slots=True)
class ClaudeSignInPrompt:
    sign_in_id: str
    verification_uri: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class ClaudeAuthStatus:
    """``claude auth status --json``'s three fields, and nothing else it reports."""

    logged_in: bool
    auth_method: str | None
    subscription_type: str | None


@dataclass(eq=False)
class _WaitingSignIn:
    sign_in_id: str
    contract_id: str
    method: str
    harness_root: Path
    uid: int | None
    process: asyncio.subprocess.Process
    master_fd: int
    deadline: float
    output: bytearray = field(default_factory=bytearray)
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    exited: asyncio.Event = field(default_factory=asyncio.Event)
    ended: bool = False


def launcher_argv(harness_root: Path, argv: Sequence[str]) -> list[str]:
    """``argv`` behind the launcher that hands it this harness root's login."""

    return ["/bin/sh", "-c", _LAUNCHER, "agentic-claude", str(harness_root), *argv]


class ClaudeSignIns:
    """Every Claude Code sign-in this Runner has started and is still waiting on."""

    def __init__(
        self,
        isolation: ContractIsolation,
        *,
        argv_by_method: dict[str, tuple[str, ...]] | None = None,
        status_argv: Sequence[str] = _STATUS_ARGV,
        prompt_timeout_seconds: float = _DEFAULT_PROMPT_TIMEOUT_SECONDS,
        window: timedelta = SIGN_IN_WINDOW,
        platform: str = sys.platform,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._isolation = isolation
        self._argv_by_method = argv_by_method or CLAUDE_SIGN_IN_ARGV
        self._status_argv = list(status_argv)
        self._prompt_timeout_seconds = prompt_timeout_seconds
        self._window = window
        self._platform = platform
        self._clock = clock
        self._waiting: dict[str, _WaitingSignIn] = {}
        self._watchers: set[asyncio.Task[None]] = set()

    def _now(self) -> float:
        return self._clock() if self._clock is not None else asyncio.get_running_loop().time()

    @property
    def waiting(self) -> int:
        return len(self._waiting)

    async def start(self, contract_id: str, *, method: str | None) -> ClaudeSignInPrompt:
        """Spawn the method's CLI as this Contract's uid; return once it prints its URL."""

        method = method or OAUTH_TOKEN
        argv = self._argv_by_method.get(method)
        if argv is None:
            raise UnknownSignInMethodError(f"no Claude Code sign-in method {method!r}")
        sandbox = self._isolation.sandbox(contract_id, runtime_kind=RUNTIME_KIND)
        # One sign-in per Contract: a second start supersedes the first, whose code the
        # person can no longer use anyway.
        for waiting in [w for w in self._waiting.values() if w.contract_id == contract_id]:
            await self._end(waiting)
        if method == CLAUDE_AI and self._platform == "darwin":
            await self._ensure_keychain(sandbox)
        session = await self._spawn(contract_id, method, argv, sandbox)
        try:
            url = await self._await_prompt(session)
        except BaseException:
            await self._end(session)
            raise
        # The relay rests on PKCE: without a verifier held by the CLI, a relayed code
        # would be a usable credential in transit (local-agents 05, item 1).
        if "code_challenge_method=S256" not in url:
            await self._end(session)
            raise ClaudeSignInError("the sign-in URL carries no S256 code challenge")
        self._waiting[session.sign_in_id] = session
        watcher = asyncio.ensure_future(self._watch(session))
        self._watchers.add(watcher)
        watcher.add_done_callback(self._watchers.discard)
        return ClaudeSignInPrompt(
            sign_in_id=session.sign_in_id,
            verification_uri=url,
            expires_at=datetime.now(UTC) + self._window,
        )

    async def relay(self, contract_id: str, sign_in_id: str, code: str) -> SignInCodeOutcome:
        """Type one relayed code into the sign-in it names, or say why not. Never retried."""

        session = self._waiting.get(sign_in_id)
        if session is None or session.contract_id != contract_id:
            return SignInCodeOutcome.UNKNOWN
        if session.exited.is_set() or self._now() >= session.deadline:
            return SignInCodeOutcome.EXPIRED
        if not _CODE_SHAPE.fullmatch(code):
            # A newline or a control character would be keystrokes, not a code.
            return SignInCodeOutcome.UNOPENABLE
        os.write(session.master_fd, code.encode("ascii"))
        await asyncio.sleep(_ENTER_DELAY_SECONDS)
        if session.exited.is_set():
            return SignInCodeOutcome.EXPIRED
        os.write(session.master_fd, b"\r")
        return SignInCodeOutcome.WRITTEN

    async def status(self, contract_id: str) -> ClaudeAuthStatus:
        """Ask the CLI, as the Contract's uid, whether it is signed in.

        The Runner opens nothing under the harness root: the launcher (the Contract's own
        process) reads the token file, and the CLI its own login. On claude 2.1.292 a
        present token reports ``loggedIn`` without contacting Anthropic, so this proves a
        token is there, not that it is still valid (local-agents 08 finds that out).
        """

        sandbox = self._isolation.sandbox(contract_id, runtime_kind=RUNTIME_KIND)
        result = await run_subprocess_exec(
            argv=launcher_argv(sandbox.harness_config_dir, self._status_argv),
            cwd=sandbox.home_dir,
            env=_harness_env(sandbox),
            stdin=None,
            timeout_seconds=_STATUS_TIMEOUT_SECONDS,
            output_limit_bytes=_OUTPUT_LIMIT_BYTES,
            sandbox=sandbox,
        )
        # `auth status` exits 1 when signed out and still prints its JSON.
        try:
            report = json.loads(result.stdout)
        except json.JSONDecodeError:
            raise ClaudeSignInError(
                f"claude auth status printed no JSON (exit {result.exit_code})"
            ) from None
        if not isinstance(report, dict):
            raise ClaudeSignInError("claude auth status printed JSON that is not an object")
        method = report.get("authMethod")
        plan = report.get("subscriptionType")
        return ClaudeAuthStatus(
            logged_in=report.get("loggedIn") is True,
            auth_method=method if isinstance(method, str) and method != "none" else None,
            subscription_type=plan if isinstance(plan, str) else None,
        )

    async def close(self) -> None:
        for session in list(self._waiting.values()):
            await self._end(session)

    async def _spawn(
        self,
        contract_id: str,
        method: str,
        argv: tuple[str, ...],
        sandbox: DirectiveSandbox,
    ) -> _WaitingSignIn:
        master_fd, slave_fd = pty.openpty()
        try:
            fcntl.ioctl(
                slave_fd, termios.TIOCSWINSZ, struct.pack("HHHH", _PTY_ROWS, _PTY_COLUMNS, 0, 0)
            )
            os.set_blocking(master_fd, False)
            try:
                process = await asyncio.create_subprocess_exec(
                    *launcher_argv(sandbox.harness_config_dir, argv),
                    cwd=sandbox.home_dir,
                    env=_harness_env(sandbox) | {"TERM": "xterm-256color"},
                    stdin=slave_fd,
                    stdout=slave_fd,
                    stderr=slave_fd,
                    start_new_session=True,
                    **sandbox.spawn_kwargs(),
                )
            except OSError as error:
                raise ClaudeSignInError(
                    f"claude failed to start: {error.__class__.__name__}"
                ) from None
        except BaseException:
            os.close(master_fd)
            raise
        finally:
            os.close(slave_fd)
        session = _WaitingSignIn(
            sign_in_id=f"si_{secrets.token_hex(16)}",
            contract_id=contract_id,
            method=method,
            harness_root=sandbox.harness_config_dir,
            uid=sandbox.uid,
            process=process,
            master_fd=master_fd,
            deadline=self._now() + self._window.total_seconds(),
        )
        asyncio.get_running_loop().add_reader(master_fd, self._read, session)
        return session

    def _read(self, session: _WaitingSignIn) -> None:
        try:
            chunk = os.read(session.master_fd, 4096)
        except BlockingIOError:
            return
        except OSError:
            # EIO: every holder of the slave side is gone, i.e. the CLI exited.
            chunk = b""
        if not chunk:
            asyncio.get_running_loop().remove_reader(session.master_fd)
            session.exited.set()
            session.changed.set()
            return
        _keep_tail(session.output, chunk)
        session.changed.set()

    async def _await_prompt(self, session: _WaitingSignIn) -> str:
        async with asyncio.timeout(self._prompt_timeout_seconds):
            while True:
                url = _authorize_url(session.output.decode("utf-8", errors="replace"))
                if url is not None:
                    return url
                if session.exited.is_set():
                    code = await session.process.wait()
                    raise ClaudeSignInError(
                        f"claude exited before printing its sign-in URL (code {code})"
                    )
                session.changed.clear()
                await session.changed.wait()

    async def _watch(self, session: _WaitingSignIn) -> None:
        remaining = max(0.0, session.deadline - self._now())
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(session.exited.wait(), timeout=remaining)
        await self._end(session)

    async def _end(self, session: _WaitingSignIn) -> None:
        """Kill the group if it is still waiting, keep what it signed in, forget the rest."""

        if session.ended:
            return
        session.ended = True
        loop = asyncio.get_running_loop()
        if not session.exited.is_set():
            loop.remove_reader(session.master_fd)
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(session.process.pid, signal.SIGKILL)
            session.exited.set()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(session.process.wait(), timeout=2)
        with contextlib.suppress(OSError):
            # The CLI's last words can still be in the master's buffer after EOF.
            while chunk := os.read(session.master_fd, 4096):
                _keep_tail(session.output, chunk)
        with contextlib.suppress(OSError):
            os.close(session.master_fd)
        session.master_fd = -1
        token_file = session.harness_root / OAUTH_TOKEN_FILE
        try:
            if session.method == OAUTH_TOKEN:
                token = _printed_token(session.output.decode("utf-8", errors="replace"))
                if token is not None:
                    await _write_private(token_file, token, session.uid)
                elif session.process.returncode == 0:
                    raise ClaudeSignInError("claude setup-token exited 0 but printed no token")
            elif session.process.returncode == 0:
                # A finished `claude_ai` sign-in replaces a long-lived token, which the
                # launcher would otherwise go on preferring.
                await _write_private(token_file, None, session.uid)
        except ClaudeSignInError as error:
            # Nobody awaits a sign-in's end; the person sees it as still signed out.
            _logger.error("Claude Code sign-in %s did not complete: %s", session.sign_in_id, error)
        finally:
            session.output.clear()
            # Last, so `waiting` falls to zero only once the token is on disk.
            if self._waiting.get(session.sign_in_id) is session:
                del self._waiting[session.sign_in_id]

    async def _ensure_keychain(self, sandbox: DirectiveSandbox) -> None:
        """A per-Contract keychain, the default of the sandbox ``HOME`` (macOS only)."""

        root = sandbox.harness_config_dir
        keychain = root / KEYCHAIN_FILE
        password_file = root / KEYCHAIN_PASSWORD_FILE
        if keychain.exists() and password_file.exists():
            return
        password = secrets.token_urlsafe(32)
        await _write_private(password_file, password, sandbox.uid)
        # The password goes in on stdin, twice (new and retyped): in argv `ps` shows it.
        commands: list[tuple[list[str], str | None]] = [
            (["security", "create-keychain", str(keychain)], f"{password}\n{password}\n"),
            # No auto-lock: the launcher unlocks before each spawn, but a Directive can
            # outlive any timeout, and a lock mid-turn blocks on a GUI prompt.
            (["security", "set-keychain-settings", str(keychain)], None),
            (["security", "list-keychains", "-d", "user", "-s", str(keychain)], None),
            (["security", "default-keychain", "-d", "user", "-s", str(keychain)], None),
        ]
        for argv, stdin in commands:
            result = await run_subprocess_exec(
                argv=argv,
                cwd=sandbox.home_dir,
                env=_harness_env(sandbox),
                stdin=stdin,
                timeout_seconds=_STATUS_TIMEOUT_SECONDS,
                output_limit_bytes=_OUTPUT_LIMIT_BYTES,
                sandbox=sandbox,
            )
            if result.exit_code != 0:
                raise ClaudeSignInError(f"security {argv[1]} failed (exit {result.exit_code})")


def _harness_env(sandbox: DirectiveSandbox) -> dict[str, str]:
    return {
        "HOME": str(sandbox.home_dir),
        "TMPDIR": str(sandbox.tmp_dir),
        "PATH": os.environ.get("PATH") or os.defpath,
        "CLAUDE_CONFIG_DIR": str(sandbox.harness_config_dir),
    }


def _authorize_url(text: str) -> str | None:
    if hyperlink := _OSC8_URL.search(text):
        return hyperlink.group(1)
    plain = _PLAIN_URL.search(_ANSI.sub("", text))
    return plain.group(0) if plain else None


def _printed_token(text: str) -> str | None:
    match = _TOKEN.search(_ANSI.sub("", text))
    return match.group(0) if match else None


def _keep_tail(output: bytearray, chunk: bytes) -> None:
    """The last ``_OUTPUT_LIMIT_BYTES``: the token is the last thing ``setup-token`` prints,
    so a cap that kept the head would drop it from a long output."""

    output.extend(chunk)
    del output[:-_OUTPUT_LIMIT_BYTES]


async def _write_private(path: Path, value: str | None, uid: int | None) -> None:
    """0600 and the Contract's from the first byte, replacing whatever was there; or, for
    ``value`` None, gone. Done as the Contract's uid (see ``_PRIVATE_FILE_SCRIPT``)."""

    identity: dict[str, object] = (
        {} if uid is None else {"user": uid, "group": uid, "extra_groups": []}
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        "-c",
        _PRIVATE_FILE_SCRIPT,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        **identity,  # type: ignore[arg-type]
    )
    await process.communicate(json.dumps([str(path), value]).encode("utf-8"))
    if process.returncode != 0:
        raise ClaudeSignInError(f"writing {path.name} failed (exit {process.returncode})")
