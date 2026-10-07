from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

_SECRET_ARGUMENT_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?i)^[A-Z0-9_-]*(api[_-]?key|token|secret|password|credential|auth)[A-Z0-9_-]*\s*="
)
_SECRET_PATH_MARKERS: Final[tuple[str, ...]] = (
    "/.codex",
    "~/.codex",
    ".codex/",
    "id_rsa",
    "id_ed25519",
    ".netrc",
    ".npmrc",
    ".pypirc",
)
_ESCAPE_PATH_MARKERS: Final[tuple[str, ...]] = (
    "/var/run/docker.sock",
    "/proc/",
    "/sys/",
    "/dev/",
    "/var/lib/kubelet",
    "/run/containerd",
)
_CONTROL_PLANE_COMMANDS: Final[frozenset[str]] = frozenset(
    {"kubectl", "helm", "docker", "podman", "nerdctl", "ctr", "terraform"}
)
_CONTROL_PLANE_HOST_MARKERS: Final[tuple[str, ...]] = (
    "agentic-os",
    "agentic-api",
    "localhost",
    "127.0.0.1",
    "kubernetes.default",
)


@dataclass(frozen=True, slots=True)
class CommandPolicy:
    """Conservative worker command policy.

    The policy intentionally allows only read/test/build-style workspace commands and denies all
    other commands by default. Additional allowlist entries may be supplied by workers without
    widening the hard-deny checks for secrets, host escape, protected branches, or control-plane
    access.

    ``runtime_program`` names the Agent Runtime Profile's own CLI (``codex``, ``claude``). Its
    argv is built by the Runner from worker settings, never by the Agent, so its shape is not
    second-guessed here — but every hard-deny check above still bites, which is the point of a
    floor applied to every Directive regardless of grants (ADR-0011 §12).
    """

    allowed_programs: frozenset[str] = field(
        default_factory=lambda: frozenset({"git", "python", "python3", "pytest", "uv"})
    )
    runtime_program: str | None = None


@dataclass(frozen=True, slots=True)
class CommandPolicyDecision:
    allowed: bool
    reason: str


def evaluate_command_policy(
    *,
    argv: list[str],
    cwd: Path,
    workspace_root: Path,
    base_branch: str,
    work_branch: str,
    policy: CommandPolicy,
) -> CommandPolicyDecision:
    """Evaluate a worker command using fail-closed hard-deny rules."""

    if not argv or not argv[0].strip():
        return _deny("command is not allowlisted")

    if not _is_relative_to(cwd, workspace_root):
        return _deny("cwd must be under workspace root")

    normalized_argv = [argument.strip() for argument in argv]
    command = Path(normalized_argv[0]).name
    lowered_arguments = [argument.lower() for argument in normalized_argv]

    if _contains_secret_argument(normalized_argv):
        return _deny("secret-looking argument is denied")

    if _contains_secret_path(lowered_arguments):
        return _deny("secret read is denied")

    if _contains_host_escape(lowered_arguments):
        return _deny("host or container escape is denied")

    if _contains_control_plane_access(command, lowered_arguments):
        return _deny("control-plane mutation or cluster command is denied")

    if _mutates_protected_branch(command, normalized_argv, base_branch=base_branch):
        return _deny("protected branch mutation is denied")

    if _contains_workspace_external_path(
        command,
        normalized_argv,
        cwd=cwd,
        workspace_root=workspace_root,
    ):
        return _deny("path argument must stay under workspace root")

    if command == policy.runtime_program:
        return CommandPolicyDecision(allowed=True, reason="allowed")

    if command not in policy.allowed_programs:
        return _deny("command is not allowlisted")

    if not _is_safe_allowlisted_command(command, normalized_argv, work_branch=work_branch):
        return _deny("command is not allowlisted")

    return CommandPolicyDecision(allowed=True, reason="allowed")


def _deny(reason: str) -> CommandPolicyDecision:
    return CommandPolicyDecision(allowed=False, reason=reason)


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
    except ValueError:
        return False
    return True


def _contains_secret_argument(argv: list[str]) -> bool:
    return any(_SECRET_ARGUMENT_PATTERN.search(argument) is not None for argument in argv)


def _contains_secret_path(lowered_arguments: list[str]) -> bool:
    return any(
        marker in argument for argument in lowered_arguments for marker in _SECRET_PATH_MARKERS
    )


def _contains_host_escape(lowered_arguments: list[str]) -> bool:
    return any(
        marker in argument for argument in lowered_arguments for marker in _ESCAPE_PATH_MARKERS
    )


def _contains_control_plane_access(command: str, lowered_arguments: list[str]) -> bool:
    if command in _CONTROL_PLANE_COMMANDS:
        return True
    if command in {"curl", "wget", "http", "httpx"}:
        return any(
            marker in argument
            for argument in lowered_arguments[1:]
            for marker in _CONTROL_PLANE_HOST_MARKERS
        )
    return False


def _mutates_protected_branch(command: str, argv: list[str], *, base_branch: str) -> bool:
    if command != "git" or len(argv) < 2 or argv[1] != "push":
        return False

    protected_refs = {
        base_branch,
        f"refs/heads/{base_branch}",
        f"HEAD:{base_branch}",
        f"HEAD:refs/heads/{base_branch}",
    }
    for argument in argv[2:]:
        refspec = argument.removeprefix("+")
        if refspec in protected_refs:
            return True
        if ":" in refspec and refspec.rsplit(":", 1)[1] in protected_refs:
            return True
    return False


def _contains_workspace_external_path(
    command: str,
    argv: list[str],
    *,
    cwd: Path,
    workspace_root: Path,
) -> bool:
    return any(
        not _is_relative_to(_resolve_command_path(argument, cwd), workspace_root)
        for argument in _path_arguments(command, argv)
    )


def _resolve_command_path(argument: str, cwd: Path) -> Path:
    path = Path(argument).expanduser()
    if path.is_absolute():
        return path
    return cwd / path


def _path_arguments(command: str, argv: list[str]) -> list[str]:
    if command == "git" and len(argv) >= 2 and argv[1] == "diff" and "--no-index" in argv:
        return [argument for argument in argv[2:] if _could_be_path(argument)]
    if command in {"python", "python3"} and len(argv) >= 3 and argv[1:3] == ["-m", "pytest"]:
        return [argument for argument in argv[3:] if _could_be_path(argument)]
    if command == "pytest":
        return [argument for argument in argv[1:] if _could_be_path(argument)]
    if (
        command == "uv"
        and len(argv) >= 3
        and argv[1:3]
        in (
            ["run", "pytest"],
            ["run", "ruff"],
            ["run", "mypy"],
        )
    ):
        return [argument for argument in argv[3:] if _could_be_path(argument)]
    return []


def _could_be_path(argument: str) -> bool:
    if not argument or argument.startswith("-") or "://" in argument:
        return False
    return argument.startswith(("/", "./", "../", "~")) or "/" in argument or "." in argument


def _is_safe_allowlisted_command(command: str, argv: list[str], *, work_branch: str) -> bool:
    if command == "git":
        return _is_safe_git_command(argv, work_branch=work_branch)
    if command in {"python", "python3"}:
        return len(argv) >= 3 and argv[1:3] == ["-m", "pytest"]
    if command == "pytest":
        return True
    if command == "uv":
        return len(argv) >= 3 and argv[1:3] in (["run", "pytest"], ["run", "ruff"], ["run", "mypy"])
    return False


def _is_safe_git_command(argv: list[str], *, work_branch: str) -> bool:
    if len(argv) < 2:
        return False
    subcommand = argv[1]
    if subcommand in {"status", "diff", "log", "show", "branch"}:
        return True
    if subcommand == "checkout" and len(argv) == 3:
        return argv[2] == work_branch
    return False
