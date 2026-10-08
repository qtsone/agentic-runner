from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import signal
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from agentic_runner.attempts import (
    PriorAttemptAliveError,
    recorded_group,
    refuse_if_prior_attempt_alive,
)
from agentic_runner_contracts.redaction import redact_secret_like_text

DEFAULT_OUTPUT_LIMIT_BYTES = 16_384
# 15 minutes: the Verifier runs the workspace's full pytest suite (path selection is
# forbidden by _validate_argv) on a CPU-limited worker pod, where the ~2-minute
# laptop wall time stretches severalfold; 300s produced false exit-124 failures.
# Must stay under run_verifier's start_to_close_timeout in workflows/ralph.py.
DEFAULT_TIMEOUT_SECONDS = 900.0
STARTUP_FAILURE_EXIT_CODE = 126
TIMEOUT_EXIT_CODE = 124
SAFE_ENV_KEYS = frozenset(("PATH", "LANG", "LC_ALL", "TZ"))
SECRET_KEY_PATTERN = re.compile(
    r"(secret|token|password|passwd|credential|apikey|api_key|key)", re.I
)
SHELL_METACHAR_PATTERN = re.compile(r"[;&|<>`$(){}\[\]*?~!#\n\r]")
PYTEST_SAFE_ARGS = frozenset(("--version", "-q", "-v"))
PNPM_COMMANDS = frozenset(("build", "lint", "test", "install"))
PNPM_CWD_OPTIONS = frozenset(("--dir", "-C", "--prefix"))


class CommandValidationError(ValueError):
    """Raised when a verifier command is outside the product-configured safety policy."""


class CommandRunner(Protocol):
    def __call__(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        env: dict[str, str],
        timeout_seconds: float,
    ) -> subprocess.CompletedProcess[str]: ...


@dataclass(frozen=True, slots=True)
class VerificationResult:
    command_hash: str
    exit_code: int
    passed: bool
    stdout: str
    stderr: str
    elapsed_ms: int
    working_directory: str
    argv: tuple[str, ...]
    env_keys: tuple[str, ...]

    def to_evidence(self) -> dict[str, object]:
        return {
            "command_hash": self.command_hash,
            "exit_code": self.exit_code,
            "passed": self.passed,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "elapsed_ms": self.elapsed_ms,
            "working_directory": self.working_directory,
            "argv": self.argv,
            "env_keys": self.env_keys,
        }


def _subprocess_runner(
    argv: tuple[str, ...],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout_seconds: float,
    preexec_fn: Callable[[], None] | None = None,
    user: int | None = None,
    group: int | None = None,
    extra_groups: list[int] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Spawn the verifier.

    The last four carry the Contract's sandbox floor (ADR-0015 §1): the verifier runs the
    repository's own test suite, so it is one of the things that must run as the
    Contract's uid and under its rlimits, not as the Runner. They are optional so a Runner
    that cannot separate uids still verifies, and they arrive together from
    ``DirectiveSandbox.spawn_kwargs`` — the uid drop in subprocess's own C fork-exec path,
    only the rlimits in the Python hook.

    Its own session, killed as a group on timeout, like a Directive: the test suite
    forks workers into the Workspace, and the attempt fence (``attempts.py``) records
    the group by its pgid.
    """

    refuse_if_prior_attempt_alive()
    with subprocess.Popen(
        list(argv),
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        preexec_fn=preexec_fn,
        user=user,
        group=group,
        extra_groups=extra_groups,
    ) as process:
        try:
            with recorded_group(process.pid):
                try:
                    stdout, stderr = process.communicate(timeout=timeout_seconds)
                except subprocess.TimeoutExpired:
                    _kill_group(process)
                    raise
        except PriorAttemptAliveError:
            _kill_group(process)
            raise
    return subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)


def _kill_group(process: subprocess.Popen[str]) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGKILL)
    process.kill()
    process.communicate()


def run(
    argv: tuple[str, ...],
    *,
    working_directory: Path,
    workspace_root: Path,
    safe_env: Mapping[str, str] | None = None,
    output_limit_bytes: int = DEFAULT_OUTPUT_LIMIT_BYTES,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    runner: CommandRunner = _subprocess_runner,
) -> VerificationResult:
    if output_limit_bytes < 0:
        raise CommandValidationError("output limit must be non-negative")
    if timeout_seconds <= 0:
        raise CommandValidationError("timeout must be positive")

    validated_argv = _validate_argv(argv)
    resolved_cwd = _validate_working_directory(working_directory, workspace_root)
    env, redaction_values = _build_env(safe_env)
    redaction_values = redaction_values | _secret_values_from_argv(validated_argv)
    redacted_argv = _redact_argv(validated_argv)
    command_hash = _command_hash(validated_argv, resolved_cwd)

    start = time.monotonic()
    try:
        completed = runner(
            validated_argv,
            cwd=resolved_cwd,
            env=env,
            timeout_seconds=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        elapsed_ms = max(0, int((time.monotonic() - start) * 1000))
        return _failed_result(
            command_hash=command_hash,
            exit_code=TIMEOUT_EXIT_CODE,
            stderr=f"command timed out after {exc.timeout:g} seconds",
            elapsed_ms=elapsed_ms,
            working_directory=resolved_cwd,
            argv=redacted_argv,
            env=env,
            redaction_values=redaction_values,
            output_limit_bytes=output_limit_bytes,
        )
    except OSError as exc:
        elapsed_ms = max(0, int((time.monotonic() - start) * 1000))
        return _failed_result(
            command_hash=command_hash,
            exit_code=STARTUP_FAILURE_EXIT_CODE,
            stderr=f"command startup failed: {type(exc).__name__}",
            elapsed_ms=elapsed_ms,
            working_directory=resolved_cwd,
            argv=redacted_argv,
            env=env,
            redaction_values=redaction_values,
            output_limit_bytes=output_limit_bytes,
        )
    elapsed_ms = max(0, int((time.monotonic() - start) * 1000))

    return VerificationResult(
        command_hash=command_hash,
        exit_code=completed.returncode,
        passed=completed.returncode == 0,
        stdout=_truncate(
            _redact_persisted_output(completed.stdout, redaction_values),
            output_limit_bytes,
        ),
        stderr=_truncate(
            _redact_persisted_output(completed.stderr, redaction_values),
            output_limit_bytes,
        ),
        elapsed_ms=elapsed_ms,
        working_directory=str(resolved_cwd),
        argv=redacted_argv,
        env_keys=tuple(sorted(env)),
    )


def _failed_result(
    *,
    command_hash: str,
    exit_code: int,
    stderr: str,
    elapsed_ms: int,
    working_directory: Path,
    argv: tuple[str, ...],
    env: dict[str, str],
    redaction_values: frozenset[str],
    output_limit_bytes: int,
) -> VerificationResult:
    return VerificationResult(
        command_hash=command_hash,
        exit_code=exit_code,
        passed=False,
        stdout="",
        stderr=_truncate(_redact_persisted_output(stderr, redaction_values), output_limit_bytes),
        elapsed_ms=elapsed_ms,
        working_directory=str(working_directory),
        argv=argv,
        env_keys=tuple(sorted(env)),
    )


def _validate_argv(argv: tuple[str, ...]) -> tuple[str, ...]:
    if not argv:
        raise CommandValidationError("command argv must not be empty")

    for token in argv:
        if token == "" or SHELL_METACHAR_PATTERN.search(token):
            raise CommandValidationError(
                "shell metacharacters are not allowed in verifier commands"
            )
        if " " in token and not token.startswith("--"):
            raise CommandValidationError("compound shell command strings are not allowed")
        if token in {".", ".."}:
            raise CommandValidationError("path selector arguments are not allowed")
        if "/" in token or "\\" in token:
            raise CommandValidationError("path-like argv entries are not allowed")

    executable = argv[0]
    if executable == "python" and len(argv) >= 3 and argv[1] == "-m" and argv[2] == "pytest":
        return _validate_pytest_argv(argv)
    if executable == "pnpm" and len(argv) >= 2 and argv[1] in PNPM_COMMANDS:
        return _validate_pnpm_argv(argv)
    if executable == "npm" and argv == ("npm", "--version"):
        return argv

    raise CommandValidationError(f"command binary or argv shape is not allowed: {executable}")


def _validate_pytest_argv(argv: tuple[str, ...]) -> tuple[str, ...]:
    for token in argv[3:]:
        option, separator, value = token.partition("=")
        if option in {"--rootdir", "--confcutdir", "--basetemp"}:
            raise CommandValidationError("pytest cwd or path selector arguments are not allowed")
        if separator and value in {".", ".."}:
            raise CommandValidationError("pytest path selector arguments are not allowed")
        if token not in PYTEST_SAFE_ARGS:
            raise CommandValidationError("pytest argument is not allowed by verifier schema")
    return argv


def _validate_pnpm_argv(argv: tuple[str, ...]) -> tuple[str, ...]:
    index = 2
    while index < len(argv):
        token = argv[index]
        option, separator, value = token.partition("=")
        if token in PNPM_CWD_OPTIONS or option in PNPM_CWD_OPTIONS:
            raise CommandValidationError("pnpm cwd-changing arguments are not allowed")
        if separator and value in {".", ".."}:
            raise CommandValidationError("pnpm path selector arguments are not allowed")
        if option == "--token" and separator and value:
            index += 1
            continue
        raise CommandValidationError("pnpm argument is not allowed by verifier schema")
    return argv


def _validate_working_directory(working_directory: Path, workspace_root: Path) -> Path:
    if ".." in working_directory.parts:
        raise CommandValidationError("working directory must not contain path traversal")

    resolved_root = workspace_root.resolve(strict=True)
    resolved_cwd = working_directory.resolve(strict=True)
    if resolved_cwd != resolved_root and resolved_root not in resolved_cwd.parents:
        raise CommandValidationError("working directory must be inside workspace root")
    return resolved_cwd


def _build_env(safe_env: Mapping[str, str] | None) -> tuple[dict[str, str], frozenset[str]]:
    env: dict[str, str] = {}
    path = os.environ.get("PATH")
    if path:
        env["PATH"] = path

    redaction_values: set[str] = set()
    for key, value in (safe_env or {}).items():
        if SECRET_KEY_PATTERN.search(key):
            if value:
                redaction_values.add(value)
            continue
        if key in SAFE_ENV_KEYS or key.startswith("SAFE_"):
            env[key] = value

    return env, frozenset(redaction_values)


def _secret_values_from_argv(argv: tuple[str, ...]) -> frozenset[str]:
    values: set[str] = set()
    for token in argv:
        option, separator, value = token.partition("=")
        if separator and SECRET_KEY_PATTERN.search(option) and value:
            values.add(value)
    return frozenset(values)


def _redact_argv(argv: tuple[str, ...]) -> tuple[str, ...]:
    redacted: list[str] = []
    for token in argv:
        option, separator, value = token.partition("=")
        if separator and SECRET_KEY_PATTERN.search(option) and value:
            redacted.append(f"{option}=[REDACTED]")
        else:
            redacted.append(token)
    return tuple(redacted)


def _redact(value: str, secrets: frozenset[str]) -> str:
    redacted = value
    for secret in sorted(secrets, key=len, reverse=True):
        if secret:
            redacted = redacted.replace(secret, "[REDACTED]")
    return redacted


def _redact_persisted_output(value: str, secrets: frozenset[str]) -> str:
    return redact_secret_like_text(_redact(value, secrets))


def _truncate(value: str, limit_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= limit_bytes:
        return value

    prefix = encoded[:limit_bytes].decode("utf-8", errors="ignore")
    return f"{prefix}...[truncated {len(encoded) - limit_bytes} bytes]"


def _command_hash(argv: tuple[str, ...], cwd: Path) -> str:
    payload = json.dumps(
        {"argv": argv, "working_directory": str(cwd)},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
