from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import Awaitable, Callable
from typing import Final

from agentic_runner.workers._runtime_support import (
    SubprocessResult,
    apply_extra_env,
    bound_text,
    command_policy_refusal,
    hash_command,
    is_canonical_uuid,
    is_relative_to,
    redact_match,
    run_subprocess_exec,
    workspace_id,
)
from agentic_runner.workers.agent_runtime import (
    AuthMode,
    DirectiveEvidence,
    DirectiveRequest,
    DirectiveResult,
    ResumableAgentRuntime,
)
from agentic_runner.workers.contract_isolation import DirectiveSandbox
from agentic_runner.workers.mcp_config import claude_mcp_config
from agentic_runner.workers.settings import WorkerSettings

__all__ = ["ClaudeRuntime"]

_SECRET_VALUE_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(
        r"(?i)((?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|password|credential)\s*=\s*)([^\s,}]+)"
    ),
    re.compile(
        r"(?i)((?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|password|credential)\s*:\s*)([^\s,}]+)"
    ),
    re.compile(
        r"(?i)((?:\"(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|password|credential)\"\s*:\s*\"))([^\"]+)(\")"
    ),
    re.compile(r"(?i)((?:--password|--token|--secret|--api-key)\s+)([^\s]+)"),
    re.compile(r"(?i)(authorization:\s*bearer\s+)([^\s]+)"),
    re.compile(r"(?i)(bearer\s+)([A-Za-z0-9._~+/-]{8,})"),
    re.compile(r"(?i)(sk-[A-Za-z0-9_-]{8,})"),
    re.compile(r"(?i)(?:~|/[^\s]*)/\.claude(?:/[^\s-]+)?"),
    re.compile(r"(?is)-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)


class ClaudeRuntime(ResumableAgentRuntime):
    """Worker-local Claude Code CLI runtime.

    Authenticates with an API key (``ANTHROPIC_API_KEY``) rather than a device-login session,
    so an unattended Persona can run without an expiring browser session (ADR-0006). Like the
    Codex runtime it confines execution to the supplied workspace, refuses fail-closed unless the
    autonomous-permission guard is configured, and returns bounded, redacted evidence safe for
    control-plane storage — the API key never appears in returned text.
    """

    # Subscription mode for Claude Code is local-agents 07; until then a user-hosted Runner
    # with no key runs it on the host operator's key, as before.
    auth_modes = frozenset({AuthMode.API_KEY})

    def __init__(
        self,
        *,
        settings: WorkerSettings,
        runner: Callable[..., Awaitable[SubprocessResult]] | None = None,
    ) -> None:
        self._settings = settings
        self._runner = runner or run_subprocess_exec
        self.host_api_key = bool(settings.ANTHROPIC_API_KEY)

    async def execute_directive(self, request: DirectiveRequest) -> DirectiveResult:
        workspace_path = request.workspace_path.resolve(strict=False)
        workspace_root = self._settings.WORKSPACE_ROOT.resolve(strict=False)
        if not is_relative_to(workspace_path, workspace_root):
            raise ValueError("workspace_path must be under WORKSPACE_ROOT")

        directive_workspace_id = workspace_id(workspace_path, workspace_root)
        guard_mode = _guard_mode(self._settings)
        if guard_mode is None:
            return _refused_result(
                workspace_id=directive_workspace_id,
                base_branch=request.base_branch,
                work_branch=request.work_branch,
                evidence_limit_bytes=self._settings.CLAUDE_CLI_OUTPUT_LIMIT_BYTES,
            )

        argv = _claude_argv(self._settings)
        if request.mcp_servers is not None:
            # Only what the Grant let through: `--strict-mcp-config` ignores every other
            # MCP source, so nothing in the harness root can add an ungranted server.
            if request.mcp_servers:
                argv += ["--mcp-config", claude_mcp_config(request.mcp_servers)]
            argv.append("--strict-mcp-config")
        # Hashed without the session arguments: a fresh session id is new on every run,
        # and the hash names the command, not the run.
        command_hash = hash_command(argv)
        started_session: str | None = None
        if request.resume_session_id is not None:
            argv += ["--resume", request.resume_session_id]
        elif request.on_session_started is not None:
            # Chosen here rather than read back from the output, which only names the
            # session once the turn is over -- too late for an attempt lost mid-turn.
            started_session = str(uuid.uuid4())
            argv += ["--session-id", started_session]
        floor_refusal = command_policy_refusal(
            argv=argv,
            program="claude",
            workspace_path=workspace_path,
            workspace_root=workspace_root,
            base_branch=request.base_branch,
            work_branch=request.work_branch,
        )
        if floor_refusal is not None:
            return _refused_result(
                workspace_id=directive_workspace_id,
                base_branch=request.base_branch,
                work_branch=request.work_branch,
                evidence_limit_bytes=self._settings.CLAUDE_CLI_OUTPUT_LIMIT_BYTES,
                guard_mode=f"refused: command policy ({floor_refusal})",
            )
        # Per-Contract CLAUDE_CONFIG_DIR, never a fleet-wide one (ADR-0015 §4).
        sandbox = request.sandbox
        env = {"ANTHROPIC_API_KEY": self._settings.ANTHROPIC_API_KEY}
        if sandbox is not None:
            env["CLAUDE_CONFIG_DIR"] = str(sandbox.harness_config_dir)
            env["HOME"] = str(sandbox.home_dir)
            env["TMPDIR"] = str(sandbox.tmp_dir)
        apply_extra_env(env, request.extra_env)
        notes: list[str] = []
        if started_session is not None and request.on_session_started is not None:
            request.on_session_started(started_session)

        try:
            raw_result = await self._runner(
                argv=argv,
                cwd=workspace_path,
                env=env,
                stdin=request.prompt,
                timeout_seconds=self._settings.CLAUDE_CLI_TIMEOUT_SECONDS,
                output_limit_bytes=self._settings.CLAUDE_CLI_OUTPUT_LIMIT_BYTES,
                sandbox=sandbox,
            )
            error = ""
        except asyncio.CancelledError:
            raise
        except TimeoutError as exception:
            raw_result = SubprocessResult(exit_code=124, stdout="", stderr="")
            error = str(exception) or "Claude Code CLI timed out"
        except OSError as exception:
            raw_result = SubprocessResult(exit_code=127, stdout="", stderr="")
            error = str(exception)

        limit_bytes = self._settings.CLAUDE_CLI_OUTPUT_LIMIT_BYTES
        stdout = bound_text(_redact_text(raw_result.stdout), limit_bytes, notes, "stdout")
        if raw_result.stdout_truncated:
            notes.append(f"stdout truncated to {limit_bytes} bytes")
        stderr = bound_text(_redact_text(raw_result.stderr), limit_bytes, notes, "stderr")
        if raw_result.stderr_truncated:
            notes.append(f"stderr truncated to {limit_bytes} bytes")
        redacted_error = bound_text(_redact_text(error), limit_bytes, notes, "error")

        evidence = DirectiveEvidence(
            workspace_id=_redact_text(directive_workspace_id),
            base_branch=_redact_evidence_field(request.base_branch, limit_bytes),
            work_branch=_redact_evidence_field(request.work_branch, limit_bytes),
            command_hash=command_hash,
            guard_mode=guard_mode,
            notes=notes,
        )
        return DirectiveResult(
            exit_code=raw_result.exit_code,
            stdout=stdout,
            stderr=stderr,
            error=redacted_error,
            command_hash=command_hash,
            evidence=evidence,
        )

    def has_session(self, session_id: str, sandbox: DirectiveSandbox | None) -> bool:
        # Without a Contract sandbox the child gets no CLAUDE_CONFIG_DIR or HOME, so
        # there is no known place its session was kept.
        if sandbox is None or not is_canonical_uuid(session_id):
            return False
        projects = sandbox.harness_config_dir / "projects"
        return any(projects.glob(f"*/{session_id}.jsonl"))


def _guard_mode(settings: WorkerSettings) -> str | None:
    if settings.ANTHROPIC_API_KEY and settings.CLAUDE_DANGEROUSLY_SKIP_PERMISSIONS:
        return "auth=api-key; permission=skip-in-sandbox; output=json"
    return None


def _claude_argv(settings: WorkerSettings) -> list[str]:
    return [
        "claude",
        "--print",
        "--bare",
        "--output-format",
        "json",
        "--model",
        settings.CLAUDE_MODEL,
        "--dangerously-skip-permissions",
    ]


def _refused_result(
    *,
    workspace_id: str,
    base_branch: str,
    work_branch: str,
    evidence_limit_bytes: int,
    guard_mode: str = "refused: missing API key or autonomous permission configuration",
    error: str = (
        "Claude Code API key and autonomous permission configuration are required "
        "before worker-local execution"
    ),
) -> DirectiveResult:
    command_hash = hash_command(["claude", "refused"])
    return DirectiveResult(
        exit_code=126,
        stdout="",
        stderr="",
        error=error,
        command_hash=command_hash,
        evidence=DirectiveEvidence(
            workspace_id=_redact_text(workspace_id),
            command_hash=command_hash,
            guard_mode=guard_mode,
            base_branch=_redact_evidence_field(base_branch, evidence_limit_bytes),
            work_branch=_redact_evidence_field(work_branch, evidence_limit_bytes),
            notes=[],
        ),
    )


def _redact_text(value: str) -> str:
    redacted = value
    for pattern in _SECRET_VALUE_PATTERNS:
        redacted = pattern.sub(redact_match, redacted)
    return redacted


def _redact_evidence_field(value: str, limit_bytes: int) -> str:
    return bound_text(_redact_text(value), limit_bytes, [], "evidence")
