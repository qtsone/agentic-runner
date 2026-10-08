"""Runtime-neutral machinery shared by Agent Runtimes.

The subprocess spawn (with process-group kill on timeout/cancel), workspace-under-root
validation, command hashing, and byte-bounding here are identical for every CLI runtime
(Codex, Claude Code, ...). Each runtime keeps its own secret-redaction patterns, since
the secrets it must scrub differ; this module holds only what is genuinely shared.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import re
import signal
import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from agentic_runner.attempts import (
    PriorAttemptAliveError,
    recorded_group,
    refuse_if_prior_attempt_alive,
)
from agentic_runner.callback import CALLBACK_SOCKET_ENV, CALLBACK_TOKEN_ENV
from agentic_runner.egress import EGRESS_ENV_NAMES
from agentic_runner.llm_proxy import PROXY_ENV_NAMES
from agentic_runner.workers.command_policy import CommandPolicy, evaluate_command_policy
from agentic_runner.workers.contract_isolation import DirectiveSandbox

# Names no caller may set on a Directive's environment, whatever a Runner Hook exported
# (PRD issue 45). Each is a line the Runner itself draws: the per-Contract harness root
# and HOME (ADR-0015 §4), the TMPDIR inside the Contract's tree, the PATH the harness
# binary resolves on, the attempt's own callback socket and bearer, and the LLM proxy's
# endpoint and bearer (PRD issue 43), and the attempt's egress proxy (PRD issue 58). An
# `environment` hook that could rewrite them would move the Contract's config root
# somewhere another Contract can read, hand the Agent a socket and bearer of its own
# choosing, point the Agent's traffic at an endpoint that meters nothing and is charged to
# the funder anyway, or route it round the Profile's egress allow-list. The two Claude Code
# bearers are here for the last reason too: either would authenticate the harness on a
# credential the Runner never chose, around the proxy and the mode it decided
# (local-agents 04; research 01 §4 gap 6).
RESERVED_DIRECTIVE_ENV: Final[frozenset[str]] = frozenset(
    {
        "HOME",
        "PATH",
        "TMPDIR",
        "CODEX_HOME",
        "CLAUDE_CONFIG_DIR",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
        CALLBACK_SOCKET_ENV,
        CALLBACK_TOKEN_ENV,
        *PROXY_ENV_NAMES,
        *EGRESS_ENV_NAMES,
    }
)


# The callback pair and the proxy pair are minted by the Runner and travel on this same
# channel, so they are the part of the reserved set that may land here. A *hook* is
# stopped from claiming those names earlier and elsewhere — `hooks._read_env_file` drops
# them as it reads the exports — which keeps one rule in each place instead of one
# conditional in both.
_EXTRA_ENV_DENYLIST: Final[frozenset[str]] = RESERVED_DIRECTIVE_ENV - {
    CALLBACK_SOCKET_ENV,
    CALLBACK_TOKEN_ENV,
    *PROXY_ENV_NAMES,
    *EGRESS_ENV_NAMES,
}


# Provider keys a runtime may otherwise put in the child environment. Dropped whenever
# the attempt routes through the Runner's LLM proxy instead.
_PROVIDER_KEY_ENV: Final[frozenset[str]] = frozenset({"ANTHROPIC_API_KEY", "OPENAI_API_KEY"})

# A harness's opening event is one short JSON line; past this it is not one.
_FIRST_LINE_LIMIT_BYTES: Final[int] = 64 * 1024


def apply_extra_env(env: dict[str, str], extra: Iterable[tuple[str, str]]) -> dict[str, str]:
    """Merge one attempt's extra environment into a Directive's, minus the reserved names.

    Where the attempt carries an LLM proxy endpoint (PRD issue 43), the runtime's own
    provider key is dropped from the child environment rather than left beside it: the
    whole point of the relocated proxy is that the subprocess holds the attempt's bearer
    and no provider key (ADR-0011 §9), and a key still in the env is a way around the
    metering point. Done here because both runtimes route through this one call.
    """

    admitted = {name: value for name, value in extra if name not in _EXTRA_ENV_DENYLIST}
    if PROXY_ENV_NAMES & set(admitted):
        for name in _PROVIDER_KEY_ENV:
            env.pop(name, None)
    env.update(admitted)
    return env


@dataclass(frozen=True, slots=True)
class SubprocessResult:
    exit_code: int
    stdout: str
    stderr: str
    stdout_truncated: bool = False
    stderr_truncated: bool = False


AsyncSubprocessRunner = Callable[
    [list[str], Path, dict[str, str], str | None, int, int], Awaitable[SubprocessResult]
]


async def run_subprocess_exec(
    *,
    argv: list[str],
    cwd: Path,
    env: dict[str, str],
    stdin: str | None,
    timeout_seconds: int,
    output_limit_bytes: int,
    sandbox: DirectiveSandbox | None = None,
    on_first_stdout_line: Callable[[bytes], None] | None = None,
) -> SubprocessResult:
    refuse_if_prior_attempt_alive()
    process = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
        **({} if sandbox is None else sandbox.spawn_kwargs()),
    )
    try:
        with recorded_group(process.pid):
            try:
                (
                    stdout_bytes,
                    stderr_bytes,
                    stdout_truncated,
                    stderr_truncated,
                ) = await asyncio.wait_for(
                    _collect_process_output(
                        process,
                        stdin=stdin,
                        output_limit_bytes=output_limit_bytes,
                        on_first_stdout_line=on_first_stdout_line,
                    ),
                    timeout=timeout_seconds,
                )
            except (TimeoutError, asyncio.CancelledError):
                await _terminate_process_tree(process)
                raise
    except PriorAttemptAliveError:
        await _terminate_process_tree(process)
        raise

    return SubprocessResult(
        exit_code=process.returncode or 0,
        stdout=stdout_bytes.decode(errors="replace"),
        stderr=stderr_bytes.decode(errors="replace"),
        stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_truncated,
    )


async def _collect_process_output(
    process: asyncio.subprocess.Process,
    *,
    stdin: str | None,
    output_limit_bytes: int,
    on_first_stdout_line: Callable[[bytes], None] | None = None,
) -> tuple[bytes, bytes, bool, bool]:
    if process.stdin is not None:
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            if stdin is not None:
                process.stdin.write(stdin.encode())
                await process.stdin.drain()
            process.stdin.close()
            await process.stdin.wait_closed()

    stdout_task = asyncio.create_task(
        _read_stream_limited(process.stdout, output_limit_bytes, on_first_stdout_line)
    )
    stderr_task = asyncio.create_task(_read_stream_limited(process.stderr, output_limit_bytes))
    try:
        await process.wait()
        stdout_bytes, stdout_truncated = await stdout_task
        stderr_bytes, stderr_truncated = await stderr_task
    except BaseException:
        stdout_task.cancel()
        stderr_task.cancel()
        raise
    return stdout_bytes, stderr_bytes, stdout_truncated, stderr_truncated


async def _read_stream_limited(
    stream: asyncio.StreamReader | None,
    limit_bytes: int,
    on_first_line: Callable[[bytes], None] | None = None,
) -> tuple[bytes, bool]:
    """Read a child's stream, bounded to the *last* ``limit_bytes``.

    Keeping the head instead of the tail here silently dropped Codex's ``turn.completed``
    and Claude Code's ``result`` message on any Directive whose output crossed the limit —
    both harnesses put their final, JSONL/JSON usage event at the end of stdout
    (``harness_usage.py``), and the head-bounded runtime evidence layer (``bound_text_tail``
    in ``codex_runtime.py``/``claude_runtime.py``) already assumed the tail was what
    survived. There is nothing left for a caller-side tail-bound to recover once the head
    is what got kept here, so the read itself must keep the tail.
    """

    if stream is None:
        return b"", False

    tail = bytearray()
    total_bytes = 0
    first_line = bytearray()
    while True:
        chunk = await stream.read(4096)
        if not chunk:
            break
        if on_first_line is not None:
            first_line.extend(chunk)
            line, newline, _ = first_line.partition(b"\n")
            if newline:
                on_first_line(bytes(line))
                on_first_line = None
            elif len(first_line) > _FIRST_LINE_LIMIT_BYTES:
                on_first_line = None
        total_bytes += len(chunk)
        tail.extend(chunk)
        if len(tail) > limit_bytes:
            del tail[: len(tail) - limit_bytes]
    return bytes(tail), total_bytes > limit_bytes


async def _terminate_process_tree(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    if process.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(process.wait(), timeout=2)


def is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def is_canonical_uuid(value: str) -> bool:
    # Canonical form only: a harness session id becomes part of a glob pattern.
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


def workspace_id(workspace_path: Path, workspace_root: Path) -> str:
    return workspace_path.relative_to(workspace_root).as_posix()


def hash_command(argv: list[str]) -> str:
    return hashlib.sha256("\0".join(argv).encode()).hexdigest()


def redact_match(match: re.Match[str]) -> str:
    if match.lastindex and match.lastindex >= 3:
        return f"{match.group(1)}[REDACTED]{match.group(3)}"
    if match.lastindex and match.lastindex >= 2:
        return f"{match.group(1)}[REDACTED]"
    return "[REDACTED]"


def bound_text(value: str, limit_bytes: int, notes: list[str], label: str) -> str:
    encoded = value.encode()
    if len(encoded) <= limit_bytes:
        return value

    notes.append(f"{label} truncated to {limit_bytes} bytes")
    return encoded[:limit_bytes].decode(errors="ignore")


def bound_text_tail(value: str, limit_bytes: int, notes: list[str], label: str) -> str:
    """Like ``bound_text`` but keeps the *end* of the text — where test runners put
    the failure summary and tracebacks that a fix Directive actually needs."""
    encoded = value.encode()
    if len(encoded) <= limit_bytes:
        return value

    notes.append(f"{label} truncated to last {limit_bytes} bytes")
    return encoded[-limit_bytes:].decode(errors="ignore")


def command_policy_refusal(
    *,
    argv: list[str],
    program: str,
    workspace_path: Path,
    workspace_root: Path,
    base_branch: str,
    work_branch: str,
) -> str | None:
    """The Agent Runtime Profile's sandbox floor, applied to every Directive (ADR-0011 §12).

    The subprocess the Runner is about to spawn is checked against
    ``workers/command_policy.py`` before it runs: outside the grant model, not attenuable by
    the user, and re-checked per Directive rather than once at configuration time, because
    the worker settings that build the argv can change under a running worker. Returns the
    refusal reason, or None when the floor allows the spawn.
    """

    decision = evaluate_command_policy(
        argv=argv,
        cwd=workspace_path,
        workspace_root=workspace_root,
        base_branch=base_branch,
        work_branch=work_branch,
        policy=CommandPolicy(runtime_program=program),
    )
    return None if decision.allowed else decision.reason


# Process-global strong references to detached launches' background drain tasks. A
# request/activity-scoped call is otherwise the only strong root, and task -> coroutine ->
# process is a cycle the garbage collector can reap once the caller returns (the event
# loop keeps only weak references to tasks) -- rooting them here keeps a detached child
# draining and reaped for as long as it runs.
_DETACHED_LAUNCH_DRAINS: set[asyncio.Task[None]] = set()


async def run_subprocess_launch_and_detach(
    *,
    argv: list[str],
    cwd: Path,
    env: dict[str, str],
    is_prompt_complete: Callable[[str], bool],
    prompt_timeout_seconds: float,
    output_limit_bytes: int,
    sandbox: DirectiveSandbox | None = None,
) -> str:
    """Spawn a long-running interactive CLI, return once its opening prompt is complete.

    Unlike :func:`run_subprocess_exec` (run-to-completion), a device-code sign-in prints a
    verification URL and one-time code and then *blocks*, polling the vendor until the
    operator finishes the browser flow or the code expires (research/29). The caller needs
    only the prompt, not that wait: reading it in an ``execute_workflow`` call held the
    HTTP request (and, with no override, a 60s client timeout) open for however long the
    operator took, and the funder learned the URL/code only after the whole wait finished
    (PRD issue 31 review). This reads stdout only until ``is_prompt_complete`` accepts it,
    then detaches the process -- draining its pipes in the background so it never blocks on
    a full buffer -- so a later, separate read can check whether it finished.
    """

    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            **({} if sandbox is None else sandbox.spawn_kwargs()),
        )
    except OSError as error:
        raise RuntimeError(f"process failed to start: {error.__class__.__name__}") from None

    if process.stdout is None:
        await _terminate_process_tree(process)
        raise RuntimeError("process stdout stream was unavailable")

    loop = asyncio.get_running_loop()
    deadline = loop.time() + prompt_timeout_seconds
    buffer = bytearray()
    try:
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError("device-login prompt did not appear before the timeout")
            try:
                chunk = await asyncio.wait_for(process.stdout.read(4096), timeout=remaining)
            except TimeoutError:
                raise TimeoutError(
                    "device-login prompt did not appear before the timeout"
                ) from None
            if not chunk:
                raise RuntimeError(
                    f"process exited before printing its prompt (code {process.returncode})"
                )
            buffer.extend(chunk)
            if len(buffer) > output_limit_bytes:
                raise RuntimeError("process output exceeded the configured byte limit")
            text = buffer.decode("utf-8", errors="replace")
            if is_prompt_complete(text):
                _detach(process)
                return text
    except BaseException:
        await _terminate_process_tree(process)
        raise


def _detach(process: asyncio.subprocess.Process) -> None:
    # Static target (no closure over caller state) so the drain coroutine roots only
    # through the module-level set below, not through whatever scheduled this launch.
    task = asyncio.ensure_future(_drain_detached(process))
    _DETACHED_LAUNCH_DRAINS.add(task)
    task.add_done_callback(_DETACHED_LAUNCH_DRAINS.discard)


async def _drain_detached(process: asyncio.subprocess.Process) -> None:
    # Best-effort: keeps the detached child's pipes empty so it never blocks on a full
    # buffer, then reaps it. A failure here must never surface into a caller that already
    # returned the prompt.
    try:
        streams = [s for s in (process.stdout, process.stderr) if s is not None]
        await asyncio.gather(*(_drain_stream(stream) for stream in streams))
        await process.wait()
    except Exception:  # noqa: BLE001 - best-effort background reap, see docstring above
        return


async def _drain_stream(stream: asyncio.StreamReader) -> None:
    while await stream.read(8192):
        continue
