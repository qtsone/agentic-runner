from __future__ import annotations

import os
import selectors
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from agentic_runner.integrations.git.workspace import (
    require_runner_owned_git_dir,
    resolve_workspace_path,
)
from agentic_runner_contracts.redaction import redact_secret_like_text


@dataclass(frozen=True)
class GitEvidence:
    """Bounded, redacted git status and diff evidence for worker handoff."""

    workspace_path: Path
    status: str
    diff: str
    stderr: str
    status_returncode: int
    diff_returncode: int


_MAX_UNTRACKED_FILE_BYTES: Final = 65_536
_PIPE_READ_CHUNK_BYTES: Final = 8192
_TRUNCATION_MARKER = "\n[TRUNCATED]\n"


def collect_git_evidence(
    *,
    workspace_path: Path,
    workspace_root: Path,
    output_limit_bytes: int,
    command_timeout_seconds: int = 10,
) -> GitEvidence:
    """Collect bounded, redacted status and diff from a workspace under its root."""

    if output_limit_bytes <= 0:
        raise ValueError("output_limit_bytes must be positive")
    if command_timeout_seconds <= 0:
        raise ValueError("command_timeout_seconds must be positive")

    resolved_workspace = resolve_workspace_path(workspace_root, workspace_path)
    # The other seam that runs the Runner's git over a tree the Contract owns, with the
    # same `safe.directory` suppression — `status`/`diff` honour `core.fsmonitor` and
    # `filter.*.clean` just as `commit` does.
    require_runner_owned_git_dir(resolved_workspace)
    status = _run_git_evidence_command(
        ["status", "--short", "--untracked-files=all", "--porcelain=v1"],
        cwd=resolved_workspace,
        output_limit_bytes=output_limit_bytes,
        command_timeout_seconds=command_timeout_seconds,
    )
    diff = _run_git_evidence_command(
        [
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--no-color",
            "--src-prefix=a/",
            "--dst-prefix=b/",
            "--",
        ],
        cwd=resolved_workspace,
        output_limit_bytes=output_limit_bytes,
        command_timeout_seconds=command_timeout_seconds,
    )
    staged_diff = _run_git_evidence_command(
        [
            "diff",
            "--cached",
            "--no-ext-diff",
            "--no-textconv",
            "--no-color",
            "--src-prefix=a/",
            "--dst-prefix=b/",
            "--",
        ],
        cwd=resolved_workspace,
        output_limit_bytes=output_limit_bytes,
        command_timeout_seconds=command_timeout_seconds,
    )
    untracked_files = _run_git_evidence_command(
        ["ls-files", "--others", "--exclude-standard", "-z"],
        cwd=resolved_workspace,
        output_limit_bytes=output_limit_bytes,
        command_timeout_seconds=command_timeout_seconds,
    )
    untracked_diff = _collect_untracked_text_evidence(
        workspace_path=resolved_workspace,
        git_paths=untracked_files.stdout,
        output_limit_bytes=output_limit_bytes,
    )
    combined_diff = "".join((diff.stdout, staged_diff.stdout, untracked_diff))
    return build_git_evidence(
        workspace_path=resolved_workspace,
        status=status.stdout,
        diff=combined_diff,
        stderr="\n".join(
            part
            for part in (status.stderr, diff.stderr, staged_diff.stderr, untracked_files.stderr)
            if part
        ),
        status_returncode=status.returncode,
        diff_returncode=_first_nonzero(
            diff.returncode,
            staged_diff.returncode,
            untracked_files.returncode,
        ),
        output_limit_bytes=output_limit_bytes,
    )


def build_git_evidence(
    *,
    workspace_path: Path,
    status: str,
    diff: str,
    stderr: str = "",
    status_returncode: int = 0,
    diff_returncode: int = 0,
    output_limit_bytes: int,
) -> GitEvidence:
    """Build evidence from deterministic text, applying the same redaction and limits."""

    if output_limit_bytes <= 0:
        raise ValueError("output_limit_bytes must be positive")
    return GitEvidence(
        workspace_path=workspace_path.resolve(),
        **_limit_evidence_fields_to_total_budget(
            status=redact_git_evidence_text(status),
            diff=redact_git_evidence_text(diff),
            stderr=redact_git_evidence_text(stderr),
            output_limit_bytes=output_limit_bytes,
        ),
        status_returncode=status_returncode,
        diff_returncode=diff_returncode,
    )


def redact_git_evidence_text(text: str) -> str:
    """Redact common token, credentialed URL, and private-key forms from evidence text."""

    return redact_secret_like_text(text)


@dataclass(frozen=True)
class _EvidenceCommandResult:
    stdout: str
    stderr: str
    returncode: int


def _run_git_evidence_command(
    args: list[str],
    *,
    cwd: Path,
    output_limit_bytes: int,
    command_timeout_seconds: int,
) -> _EvidenceCommandResult:
    process = subprocess.Popen(
        [
            "git",
            "-c",
            "credential.helper=",
            "-c",
            "core.fsmonitor=false",
            "-c",
            f"core.hooksPath={os.devnull}",
            # The same two the workspace adapter carries, for the same reason: this runs
            # as the Runner over a worktree owned by the Contract's uid (ADR-0015 §1).
            # Without `safe.directory` git refuses it outright with "dubious ownership";
            # without `diff.ignoreSubmodules` a repository the Directive `git init`-ed
            # inside its checkout would be descended into, and its config — which the
            # Contract owns — read by the Runner. See integrations/git/workspace._run_git.
            "-c",
            f"safe.directory={cwd}",
            "-c",
            "diff.ignoreSubmodules=dirty",
            *args,
        ],
        cwd=cwd,
        env=_git_evidence_env(),
        shell=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout, stderr = _read_process_output_with_limit(
        process,
        output_limit_bytes=output_limit_bytes,
        command_timeout_seconds=command_timeout_seconds,
    )
    return _EvidenceCommandResult(
        stdout=stdout,
        stderr=stderr,
        returncode=process.returncode if process.returncode is not None else -1,
    )


def _read_process_output_with_limit(
    process: subprocess.Popen[bytes],
    *,
    output_limit_bytes: int,
    command_timeout_seconds: int,
) -> tuple[str, str]:
    stdout_buffer = bytearray()
    stderr_buffer = bytearray()
    stdout_truncated = False
    stderr_truncated = False
    deadline = time.monotonic() + command_timeout_seconds
    selector = selectors.DefaultSelector()
    if process.stdout is not None:
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    if process.stderr is not None:
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")

    try:
        while selector.get_map():
            remaining_seconds = deadline - time.monotonic()
            if remaining_seconds <= 0:
                process.kill()
                break
            for key, _events in selector.select(timeout=remaining_seconds):
                stream = key.fileobj
                data = os.read(key.fd, _PIPE_READ_CHUNK_BYTES)
                if not data:
                    selector.unregister(stream)
                    continue
                if key.data == "stdout":
                    stdout_truncated = (
                        _append_bytes_with_limit(
                            stdout_buffer,
                            data,
                            output_limit_bytes,
                        )
                        or stdout_truncated
                    )
                else:
                    stderr_truncated = (
                        _append_bytes_with_limit(
                            stderr_buffer,
                            data,
                            output_limit_bytes,
                        )
                        or stderr_truncated
                    )
    finally:
        selector.close()

    try:
        process.wait(timeout=0)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()

    stdout = stdout_buffer.decode(errors="replace")
    stderr = stderr_buffer.decode(errors="replace")
    if stdout_truncated:
        stdout = _append_truncation_marker(stdout, output_limit_bytes)
    if stderr_truncated:
        stderr = _append_truncation_marker(stderr, output_limit_bytes)
    return stdout, stderr


def _append_bytes_with_limit(buffer: bytearray, data: bytes, limit_bytes: int) -> bool:
    remaining_bytes = limit_bytes - len(buffer)
    if remaining_bytes <= 0:
        return True
    buffer.extend(data[:remaining_bytes])
    return len(data) > remaining_bytes


def _collect_untracked_text_evidence(
    *,
    workspace_path: Path,
    git_paths: str,
    output_limit_bytes: int,
) -> str:
    evidence = ""
    for git_path in (path for path in git_paths.split("\0") if path):
        section = _build_untracked_file_section(
            workspace_path=workspace_path,
            git_path=git_path,
            output_limit_bytes=output_limit_bytes,
        )
        if section:
            evidence = _append_text_with_limit(evidence, section, output_limit_bytes)
        if len(evidence.encode()) >= output_limit_bytes:
            break
    return evidence


def _build_untracked_file_section(
    *,
    workspace_path: Path,
    git_path: str,
    output_limit_bytes: int,
) -> str:
    relative_path = Path(git_path)
    rendered_git_path = _render_synthetic_git_path(git_path)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        return _synthetic_untracked_section(git_path, "omitted unsafe untracked path")

    candidate = workspace_path / relative_path
    if candidate.is_symlink():
        return _synthetic_untracked_section(git_path, "omitted untracked symlink")

    resolved_candidate = candidate.resolve()
    if workspace_path not in resolved_candidate.parents and resolved_candidate != workspace_path:
        return _synthetic_untracked_section(git_path, "omitted untracked path outside workspace")
    if not resolved_candidate.is_file():
        return _synthetic_untracked_section(git_path, "omitted non-file untracked path")

    content = _read_untracked_text_file(
        resolved_candidate,
        max_bytes=min(output_limit_bytes, _MAX_UNTRACKED_FILE_BYTES),
    )
    if content is None:
        return _synthetic_untracked_section(git_path, "omitted binary or non-UTF-8 untracked file")

    lines = content.splitlines(keepends=True)
    prefixed_content = "".join(f"+{line}" for line in lines)
    if content and not content.endswith("\n"):
        prefixed_content += "\n\\ No newline at end of file\n"
    return (
        f"diff --git a/{rendered_git_path} b/{rendered_git_path}\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        f"+++ b/{rendered_git_path}\n"
        f"{prefixed_content}"
    )


def _read_untracked_text_file(path: Path, *, max_bytes: int) -> str | None:
    with path.open("rb") as file:
        content = file.read(max_bytes + 1)
    bounded_content = content[:max_bytes]
    if b"\0" in bounded_content:
        return None
    try:
        text = bounded_content.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if len(content) > max_bytes:
        text += "\n[TRUNCATED]\n"
    return text


def _synthetic_untracked_section(git_path: str, message: str) -> str:
    rendered_git_path = _render_synthetic_git_path(git_path)
    return (
        f"diff --git a/{rendered_git_path} b/{rendered_git_path}\n"
        "new file evidence omitted\n"
        f"# {message}\n"
    )


def _render_synthetic_git_path(git_path: str) -> str:
    rendered_characters: list[str] = []
    for character in git_path:
        codepoint = ord(character)
        if character == "\\":
            rendered_characters.append("\\\\")
        elif character == "\n":
            rendered_characters.append("\\n")
        elif character == "\r":
            rendered_characters.append("\\r")
        elif character == "\t":
            rendered_characters.append("\\t")
        elif codepoint < 32 or codepoint == 127:
            rendered_characters.append(f"\\x{codepoint:02x}")
        else:
            rendered_characters.append(character)
    return "".join(rendered_characters)


def _first_nonzero(*returncodes: int) -> int:
    for returncode in returncodes:
        if returncode != 0:
            return returncode
    return 0


def _git_evidence_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for key in ("PATH", "LANG", "LC_ALL", "LC_CTYPE"):
        value = os.environ.get(key)
        if value is not None:
            env[key] = value
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    return env


def _limit_evidence_fields_to_total_budget(
    *,
    status: str,
    diff: str,
    stderr: str,
    output_limit_bytes: int,
) -> dict[str, str]:
    remaining_bytes = output_limit_bytes
    limited_status = _limit_text_bytes(status, remaining_bytes)
    remaining_bytes -= len(limited_status.encode())
    limited_diff = _limit_text_bytes(diff, remaining_bytes)
    remaining_bytes -= len(limited_diff.encode())
    limited_stderr = _limit_text_bytes(stderr, remaining_bytes)
    return {
        "status": limited_status,
        "diff": limited_diff,
        "stderr": limited_stderr,
    }


def _append_text_with_limit(current: str, addition: str, limit_bytes: int) -> str:
    remaining_bytes = limit_bytes - len(current.encode())
    if remaining_bytes <= 0:
        return current
    return current + _limit_text_bytes(addition, remaining_bytes)


def _limit_text_bytes(text: str, limit_bytes: int) -> str:
    if limit_bytes <= 0:
        return ""
    encoded = text.encode()
    if len(encoded) <= limit_bytes:
        return text
    if limit_bytes <= len(_TRUNCATION_MARKER.encode()):
        return encoded[:limit_bytes].decode(errors="ignore")
    content_limit = limit_bytes - len(_TRUNCATION_MARKER.encode())
    return encoded[:content_limit].decode(errors="ignore") + _TRUNCATION_MARKER


def _append_truncation_marker(text: str, limit_bytes: int) -> str:
    return _limit_text_bytes(text + _TRUNCATION_MARKER, limit_bytes)
