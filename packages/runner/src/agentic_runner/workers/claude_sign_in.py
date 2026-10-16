"""Claude Code's sign-in for one Contract, run by the person on the Runner (QTS-1448).

``agentic-runner sign-in`` runs Anthropic's own CLI flow, unmodified, as the Contract's
uid in its harness root and attached to the caller's terminal. No sign-in code, token or
session passes through the control plane: the QTS-1349 decision (controls 1 and 5)
retired the sealed sign-in-code relay local-agents 05 built here.

Two methods, both made in the Contract's own harness root and neither ever delivered:

* ``oauth_token`` (the default): ``claude setup-token`` mints a long-lived (1-year)
  subscription token. The CLI prints it and exits; this module writes it to
  ``{harness_root}/oauth-token`` (0600, the Contract's uid) and drops it. It is the
  only moment the Runner process holds it (PRD decision 13 amends decision 1 for this).
* ``claude_ai``: ``claude auth login --claudeai``, a refreshing login the CLI keeps itself
  -- ``.credentials.json`` on Linux, a Keychain item on macOS.

The CLI runs on a pseudo-terminal this process relays to the caller's own, byte for byte:
``setup-token`` prints the token to its terminal and nowhere else, so the relay is the
only way to read it. The person sees and types exactly what the CLI shows and reads.

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
import os
import pty
import re
import secrets
import signal
import struct
import sys
import termios
import tty
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from agentic_runner.workers._runtime_support import run_subprocess_exec
from agentic_runner.workers.contract_isolation import ContractIsolation, DirectiveSandbox

__all__ = [
    "CLAUDE_AI",
    "CLAUDE_SIGN_IN_ARGV",
    "KEYCHAIN_FILE",
    "KEYCHAIN_PASSWORD_FILE",
    "OAUTH_TOKEN",
    "OAUTH_TOKEN_FILE",
    "SIGN_IN_METHODS",
    "ClaudeAuthStatus",
    "ClaudeSignInError",
    "ClaudeSignIns",
    "UnknownSignInMethodError",
    "launcher_argv",
    "run_attached",
]

RUNTIME_KIND: Final[str] = "claude_code"
OAUTH_TOKEN: Final[str] = "oauth_token"
CLAUDE_AI: Final[str] = "claude_ai"

CLAUDE_SIGN_IN_ARGV: Final[dict[str, tuple[str, ...]]] = {
    OAUTH_TOKEN: ("claude", "setup-token"),
    CLAUDE_AI: ("claude", "auth", "login", "--claudeai"),
}
SIGN_IN_METHODS: Final[tuple[str, ...]] = tuple(CLAUDE_SIGN_IN_ARGV)
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

# Ink wraps the authorize URL and the token at the width it is told; one line each keeps
# the token whole for `_printed_token`. The caller's terminal soft-wraps what it shows,
# which also leaves the URL copyable in one piece.
_PTY_COLUMNS: Final[int] = 1000
_PTY_ROWS: Final[int] = 50
_STATUS_TIMEOUT_SECONDS: Final[int] = 30
_OUTPUT_LIMIT_BYTES: Final[int] = 65_536

# Runs as the Contract's uid, never the Runner's: the Contract owns every directory down to
# its harness root and can swap one for a symlink while a sign-in runs. The Runner, with
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

_ANSI: Final[re.Pattern[str]] = re.compile(r"\x1b(?:\][^\x07]*\x07|\[[0-9;?>]*[A-Za-z]|[78])")
_TOKEN: Final[re.Pattern[str]] = re.compile(r"sk-ant-oat\d\d-[A-Za-z0-9_-]{20,}")


class ClaudeSignInError(RuntimeError):
    """The CLI did not start, its status could not be read, or it printed no token."""


class UnknownSignInMethodError(ValueError):
    """A ``method`` this Runner has no Claude Code sign-in command for."""


@dataclass(frozen=True, slots=True)
class ClaudeAuthStatus:
    """``claude auth status --json``'s three fields, and nothing else it reports."""

    logged_in: bool
    auth_method: str | None
    subscription_type: str | None


def launcher_argv(harness_root: Path, argv: Sequence[str]) -> list[str]:
    """``argv`` behind the launcher that hands it this harness root's login."""

    return ["/bin/sh", "-c", _LAUNCHER, "agentic-claude", str(harness_root), *argv]


class ClaudeSignIns:
    """Claude Code's sign-in and sign-in status for each Contract's harness root."""

    def __init__(
        self,
        isolation: ContractIsolation,
        *,
        argv_by_method: dict[str, tuple[str, ...]] | None = None,
        status_argv: Sequence[str] = _STATUS_ARGV,
        platform: str = sys.platform,
    ) -> None:
        self._isolation = isolation
        self._argv_by_method = argv_by_method or CLAUDE_SIGN_IN_ARGV
        self._status_argv = list(status_argv)
        self._platform = platform

    def has_harness_root(self, contract_id: str) -> bool:
        """Whether this Contract already has its Claude Code harness root (and, under
        Contract uids, its uid) here. Never the one to create either."""

        return self._isolation.existing_sandbox(contract_id, runtime_kind=RUNTIME_KIND) is not None

    async def sign_in(
        self, contract_id: str, *, method: str = OAUTH_TOKEN, stdin_fd: int = 0, stdout_fd: int = 1
    ) -> int:
        """Run the method's CLI as this Contract's uid, attached to the caller's terminal
        (``stdin_fd``/``stdout_fd``); keep what it signed in. Returns the CLI's exit code."""

        argv = self._argv_by_method.get(method)
        if argv is None:
            raise UnknownSignInMethodError(f"no Claude Code sign-in method {method!r}")
        sandbox = self._isolation.sandbox(contract_id, runtime_kind=RUNTIME_KIND)
        if method == CLAUDE_AI and self._platform == "darwin":
            await self._ensure_keychain(sandbox)
        output = bytearray()
        try:
            returncode = await run_attached(
                launcher_argv(sandbox.harness_config_dir, argv),
                sandbox,
                env=_harness_env(sandbox),
                output=output,
                stdin_fd=stdin_fd,
                stdout_fd=stdout_fd,
            )
            token_file = sandbox.harness_config_dir / OAUTH_TOKEN_FILE
            if method == OAUTH_TOKEN:
                token = _printed_token(output.decode("utf-8", errors="replace"))
                if token is not None:
                    await _write_private(token_file, token, sandbox.uid)
                elif returncode == 0:
                    raise ClaudeSignInError("claude setup-token exited 0 but printed no token")
            elif returncode == 0:
                # A finished `claude_ai` sign-in replaces a long-lived token, which the
                # launcher would otherwise go on preferring.
                await _write_private(token_file, None, sandbox.uid)
        finally:
            output.clear()
        return returncode

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


async def run_attached(
    argv: Sequence[str],
    sandbox: DirectiveSandbox,
    *,
    env: Mapping[str, str],
    output: bytearray,
    stdin_fd: int,
    stdout_fd: int,
) -> int:
    """Run ``argv`` on a pseudo-terminal relayed to the caller's; its output's tail lands
    in ``output``. Returns once the CLI has exited and its last output is relayed. Codex's
    sign-in runs through it too (``harness_logins.CodexSignIns``)."""

    master_fd, slave_fd = pty.openpty()
    try:
        _copy_rows(stdout_fd, slave_fd)
        os.set_blocking(master_fd, False)
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=sandbox.home_dir,
                env={**env, "TERM": os.environ.get("TERM") or "xterm-256color"},
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                start_new_session=True,
                **sandbox.spawn_kwargs(),
            )
        except OSError as error:
            raise ClaudeSignInError(
                f"the sign-in failed to start: {error.__class__.__name__}"
            ) from None
    except BaseException:
        os.close(master_fd)
        raise
    finally:
        os.close(slave_fd)

    loop = asyncio.get_running_loop()
    exited = asyncio.Event()

    def from_cli() -> None:
        try:
            chunk = os.read(master_fd, 4096)
        except BlockingIOError:
            return
        except OSError:
            # EIO: every holder of the slave side is gone, i.e. the CLI exited.
            chunk = b""
        if not chunk:
            loop.remove_reader(master_fd)
            exited.set()
            return
        _write_all(stdout_fd, chunk)
        _keep_tail(output, chunk)

    # What the person typed that the pty has not taken yet. A large paste fills its input
    # queue and the non-blocking master refuses the rest, so stop reading the keyboard until
    # the CLI has drained it: the paste arrives whole and this buffer stays one read long.
    pending = bytearray()
    keyboard_open = True

    def from_person() -> None:
        nonlocal keyboard_open
        typed = os.read(stdin_fd, 1024)
        if not typed:
            keyboard_open = False
            loop.remove_reader(stdin_fd)
            return
        pending.extend(typed)
        to_cli()

    def to_cli() -> None:
        try:
            del pending[: os.write(master_fd, pending)]
        except BlockingIOError:
            pass
        except OSError:
            # EIO: the CLI is gone, and `from_cli` ends the session.
            pending.clear()
        if pending:
            loop.remove_reader(stdin_fd)
            loop.add_writer(master_fd, to_cli)
            return
        loop.remove_writer(master_fd)
        if keyboard_open:
            loop.add_reader(stdin_fd, from_person)

    resizes = os.isatty(stdout_fd)
    loop.add_reader(master_fd, from_cli)
    loop.add_reader(stdin_fd, from_person)
    if resizes:
        loop.add_signal_handler(signal.SIGWINCH, _copy_rows, stdout_fd, master_fd)
    try:
        with _raw(stdin_fd):
            await exited.wait()
            return await process.wait()
    except BaseException:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)
        raise
    finally:
        if resizes:
            loop.remove_signal_handler(signal.SIGWINCH)
        loop.remove_reader(master_fd)
        loop.remove_writer(master_fd)
        loop.remove_reader(stdin_fd)
        pending.clear()
        os.close(master_fd)


def _copy_rows(caller_fd: int, pty_fd: int) -> None:
    """The caller's row count onto the CLI's pty, at ``_PTY_COLUMNS`` wide. Setting it on
    a pty that already has one is what sends the CLI its own ``SIGWINCH``."""

    rows = os.get_terminal_size(caller_fd).lines if os.isatty(caller_fd) else _PTY_ROWS
    fcntl.ioctl(pty_fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, _PTY_COLUMNS, 0, 0))


@contextlib.contextmanager
def _raw(fd: int) -> Iterator[None]:
    """The caller's terminal in raw mode, so each key reaches the CLI as the CLI's own
    terminal would deliver it (Ink reads keys, not lines), and restored after."""

    if not os.isatty(fd):
        yield
        return
    saved = termios.tcgetattr(fd)
    tty.setraw(fd)
    try:
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view) :]


def _harness_env(sandbox: DirectiveSandbox) -> dict[str, str]:
    return {
        "HOME": str(sandbox.home_dir),
        "TMPDIR": str(sandbox.tmp_dir),
        "PATH": os.environ.get("PATH") or os.defpath,
        "CLAUDE_CONFIG_DIR": str(sandbox.harness_config_dir),
    }


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
