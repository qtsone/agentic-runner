from __future__ import annotations

import asyncio
import os
import re
import uuid
from collections.abc import Awaitable, Callable
from typing import Final

from agentic_runner.llm_proxy import PROXY_ENV_NAMES
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
    REFUSED_PERMISSION_MODE,
    AuthMode,
    DirectiveEvidence,
    DirectiveRequest,
    DirectiveResult,
    ResumableAgentRuntime,
    RuntimeCapabilities,
)
from agentic_runner.workers.claude_sign_in import launcher_argv
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

    An ``api_key`` Directive runs against the attempt's LLM proxy endpoint, so the
    subprocess holds only the attempt's bearer, never a provider key (QTS-1520). A
    ``subscription`` one (local-agents 07) runs on the sign-in in the Contract's harness
    root, behind the sign-in launcher, so the token is read by the Contract's own process
    and never by the Runner's. Like the Codex runtime it confines execution to the supplied
    workspace, refuses fail-closed unless the autonomous-permission guard is configured and
    the mode's credential is in place, and returns bounded, redacted evidence safe for
    control-plane storage.
    """

    auth_modes = frozenset({AuthMode.API_KEY, AuthMode.SUBSCRIPTION})
    host_api_key = False

    def __init__(
        self,
        *,
        settings: WorkerSettings,
        runner: Callable[..., Awaitable[SubprocessResult]] | None = None,
    ) -> None:
        self._settings = settings
        self._runner = runner or run_subprocess_exec

    def capabilities(self) -> RuntimeCapabilities:
        return RuntimeCapabilities(
            auth_modes=self.auth_modes,
            permission_mode=(
                "permission=skip;output=json"
                if _guard_mode(self._settings) is not None
                else REFUSED_PERMISSION_MODE
            ),
        )

    async def execute_directive(self, request: DirectiveRequest) -> DirectiveResult:
        workspace_path = request.workspace_path.resolve(strict=False)
        workspace_root = self._settings.WORKSPACE_ROOT.resolve(strict=False)
        if not is_relative_to(workspace_path, workspace_root):
            raise ValueError("workspace_path must be under WORKSPACE_ROOT")

        directive_workspace_id = workspace_id(workspace_path, workspace_root)
        subscription = request.auth_mode == AuthMode.SUBSCRIPTION
        guard_mode = _guard_mode(self._settings, subscription=subscription)
        if guard_mode is None:
            return _refused_result(
                workspace_id=directive_workspace_id,
                base_branch=request.base_branch,
                work_branch=request.work_branch,
                evidence_limit_bytes=self._settings.CLAUDE_CLI_OUTPUT_LIMIT_BYTES,
            )
        sandbox = request.sandbox
        extra_env = request.extra_env
        if subscription:
            if sandbox is None:
                # Without the Contract's harness root the CLI would read whatever login the
                # Runner's own user holds: someone else's subscription, or none.
                return _refused_result(
                    workspace_id=directive_workspace_id,
                    base_branch=request.base_branch,
                    work_branch=request.work_branch,
                    evidence_limit_bytes=self._settings.CLAUDE_CLI_OUTPUT_LIMIT_BYTES,
                    guard_mode="refused: subscription mode without a Contract harness root",
                    error=(
                        "a subscription Claude Code Directive needs its Contract's harness root"
                    ),
                )
            # A proxy pair here would send the sign-in's bearer to the proxy, which would
            # then hold it; the activity sets none in this mode, and none gets through.
            extra_env = tuple(
                (name, value) for name, value in extra_env if name not in PROXY_ENV_NAMES
            )
        elif not dict(extra_env).get("ANTHROPIC_BASE_URL"):
            # The proxy pair is the only credential this runtime hands the harness; without
            # it the harness would fall back to whatever login or key it finds itself.
            return _refused_result(
                workspace_id=directive_workspace_id,
                base_branch=request.base_branch,
                work_branch=request.work_branch,
                evidence_limit_bytes=self._settings.CLAUDE_CLI_OUTPUT_LIMIT_BYTES,
                guard_mode="refused: api_key mode without an LLM proxy endpoint",
                error="an api_key Claude Code Directive needs the attempt's LLM proxy endpoint",
            )

        model = request.model or self._settings.CLAUDE_MODEL
        argv = _claude_argv(model, subscription=subscription)
        if request.mcp_servers is not None or subscription:
            # Only what the Grant let through: `--strict-mcp-config` ignores every other
            # MCP source, so nothing in the harness root can add an ungranted server. A
            # subscription turn always pins it: without `--bare` the CLI would otherwise
            # read the Workspace's `.mcp.json` when the Grant names no server.
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
        if subscription:
            assert sandbox is not None  # refused above
            argv = launcher_argv(sandbox.harness_config_dir, argv)
        # Per-Contract CLAUDE_CONFIG_DIR, never a fleet-wide one (ADR-0015 §4). PATH is the
        # Runner's own, as for the other runtimes: without it `claude` (in /usr/local/bin in
        # the image) and its node shebang resolve against `os.defpath` only.
        env: dict[str, str] = {"PATH": os.environ.get("PATH") or os.defpath}
        if sandbox is not None:
            env["CLAUDE_CONFIG_DIR"] = str(sandbox.harness_config_dir)
            env["HOME"] = str(sandbox.home_dir)
            env["TMPDIR"] = str(sandbox.tmp_dir)
        apply_extra_env(env, extra_env)
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
            model=model,
        )

    def has_session(self, session_id: str, sandbox: DirectiveSandbox | None) -> bool:
        # Without a Contract sandbox the child gets no CLAUDE_CONFIG_DIR or HOME, so
        # there is no known place its session was kept.
        if sandbox is None or not is_canonical_uuid(session_id):
            return False
        projects = sandbox.harness_config_dir / "projects"
        return any(projects.glob(f"*/{session_id}.jsonl"))


def _guard_mode(settings: WorkerSettings, *, subscription: bool = False) -> str | None:
    if settings.CLAUDE_DANGEROUSLY_SKIP_PERMISSIONS:
        auth = "subscription" if subscription else "api-key"
        return f"auth={auth}; permission=skip-in-sandbox; output=json"
    return None


def _claude_argv(model: str, *, subscription: bool = False) -> list[str]:
    """``--bare`` reads no OAuth login and no keychain, so a subscription turn cannot use
    it. In its place, as the ACP runtime's ``settingSources: []`` (LA-12d item 2), no setting
    source loads: the Workspace's and harness root's settings, hooks and ``CLAUDE.md`` stay
    out, so neither can set ``env``, ``apiKeyHelper`` or a base URL that would swap the
    sign-in for a key or send its bearer elsewhere. The login itself is not a setting and
    still signs the requests.
    """

    settings_flags = ["--setting-sources", ""] if subscription else ["--bare"]
    return [
        "claude",
        "--print",
        *settings_flags,
        "--output-format",
        "json",
        "--model",
        model,
        "--dangerously-skip-permissions",
    ]


def _refused_result(
    *,
    workspace_id: str,
    base_branch: str,
    work_branch: str,
    evidence_limit_bytes: int,
    guard_mode: str = "refused: missing autonomous permission configuration",
    error: str = (
        "Claude Code autonomous permission configuration is required before worker-local execution"
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
