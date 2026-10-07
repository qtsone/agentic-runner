from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Final

from agentic_runner.workers._runtime_support import (
    AsyncSubprocessRunner,
    SubprocessResult,
    apply_extra_env,
    bound_text,
    command_policy_refusal,
    hash_command,
    is_relative_to,
    redact_match,
    run_subprocess_exec,
    workspace_id,
)
from agentic_runner.workers.agent_runtime import (
    AgentRuntime,
    AuthModel,
    DirectiveEvidence,
    DirectiveRequest,
    DirectiveResult,
)
from agentic_runner.workers.mcp_config import codex_mcp_argv
from agentic_runner.workers.settings import WorkerSettings

__all__ = [
    "AsyncSubprocessRunner",
    "CodexRuntime",
    "SubprocessResult",
    "build_codex_subprocess_env",
]

# The Dockerfile installs `codex` (and its `node` interpreter) here; used as a PATH fallback
# so a bare `codex` still resolves even if the parent PATH is somehow unset.
_CODEX_INSTALL_DIR: Final[str] = "/usr/local/bin"


def build_codex_subprocess_env(codex_home: Path) -> dict[str, str]:
    """Codex child env for the worker runtime: CODEX_HOME + a resolvable PATH.

    An env with only CODEX_HOME cannot resolve the bare ``codex``
    in ``argv[0]`` (Python falls back to ``os.defpath`` = ``/bin:/usr/bin``, but codex is at
    ``/usr/local/bin/codex``), so every worker Directive exited 127. Inherit the parent PATH
    (which reaches ``/usr/local/bin`` in the pod) so both ``codex`` and its
    ``#!/usr/bin/env node`` shebang resolve, and pass HOME when set; add no parent secrets.
    """

    inherited_path = os.environ.get("PATH") or ""
    path = inherited_path or os.pathsep.join((_CODEX_INSTALL_DIR, os.defpath))
    env = {"CODEX_HOME": str(codex_home), "PATH": path}
    home = os.environ.get("HOME")
    if home:
        env["HOME"] = home
    return env


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
    re.compile(r"(?i)(?:~|/[^\s]*)/\.codex(?:/[^\s-]+)?"),
    re.compile(r"(?is)-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)


class CodexRuntime(AgentRuntime):
    """Worker-local Codex CLI runtime.

    The runtime does not accept caller-supplied secrets. It constructs the Codex environment from
    worker settings only, keeps execution inside the supplied workspace, and returns bounded
    redacted evidence suitable for control-plane storage. Codex authenticates via a device-login
    session on a worker-local PVC (CODEX_HOME).
    """

    auth_model = AuthModel.DEVICE_LOGIN

    def __init__(
        self,
        *,
        settings: WorkerSettings,
        runner: Callable[..., Awaitable[SubprocessResult]] | None = None,
    ) -> None:
        self._settings = settings
        self._runner = runner or run_subprocess_exec

    async def execute_directive(self, request: DirectiveRequest) -> DirectiveResult:
        workspace_path = request.workspace_path.resolve(strict=False)
        workspace_root = self._settings.WORKSPACE_ROOT.resolve(strict=False)
        if not is_relative_to(workspace_path, workspace_root):
            raise ValueError("workspace_path must be under WORKSPACE_ROOT")

        directive_workspace_id = workspace_id(workspace_path, workspace_root)
        allowlisted = bool(request.egress_allow_list)
        guard_mode = _guard_mode(self._settings, allowlisted=allowlisted)
        if guard_mode is None:
            return _refused_result(
                workspace_id=directive_workspace_id,
                base_branch=request.base_branch,
                work_branch=request.work_branch,
                evidence_limit_bytes=self._settings.CODEX_CLI_OUTPUT_LIMIT_BYTES,
            )

        argv = _codex_argv(self._settings, allowlisted=allowlisted)
        # Locked with no server bound too: otherwise a `[mcp_servers.*]` table in the
        # Workspace or harness-root config.toml starts a process before any model turn (LA-19b).
        argv[-1:-1] = codex_mcp_argv(request.mcp_servers or (), workspace_path)
        floor_refusal = command_policy_refusal(
            argv=argv,
            program="codex",
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
                evidence_limit_bytes=self._settings.CODEX_CLI_OUTPUT_LIMIT_BYTES,
                guard_mode=f"refused: command policy ({floor_refusal})",
            )
        command_hash = hash_command(argv)
        # The Contract's own harness config root, never the fleet-wide one (ADR-0015 §4):
        # a device login materialised into it is readable only by that Contract's uid.
        # The shared CODEX_HOME survives only as the no-isolation fallback.
        sandbox = request.sandbox
        env = build_codex_subprocess_env(
            self._settings.CODEX_HOME if sandbox is None else sandbox.harness_config_dir
        )
        if sandbox is not None:
            env["HOME"] = str(sandbox.home_dir)
            env["TMPDIR"] = str(sandbox.tmp_dir)
        apply_extra_env(env, request.extra_env)
        notes: list[str] = []

        try:
            raw_result = await self._runner(
                argv=argv,
                cwd=workspace_path,
                env=env,
                stdin=request.prompt,
                timeout_seconds=self._settings.CODEX_CLI_TIMEOUT_SECONDS,
                output_limit_bytes=self._settings.CODEX_CLI_OUTPUT_LIMIT_BYTES,
                sandbox=sandbox,
            )
            error = ""
        except asyncio.CancelledError:
            raise
        except TimeoutError as exception:
            raw_result = SubprocessResult(exit_code=124, stdout="", stderr="")
            error = str(exception) or "Codex CLI timed out"
        except OSError as exception:
            raw_result = SubprocessResult(exit_code=127, stdout="", stderr="")
            error = str(exception)

        stdout = bound_text(
            _redact_text(raw_result.stdout),
            self._settings.CODEX_CLI_OUTPUT_LIMIT_BYTES,
            notes,
            "stdout",
        )
        if raw_result.stdout_truncated:
            notes.append(f"stdout truncated to {self._settings.CODEX_CLI_OUTPUT_LIMIT_BYTES} bytes")
        stderr = bound_text(
            _redact_text(raw_result.stderr),
            self._settings.CODEX_CLI_OUTPUT_LIMIT_BYTES,
            notes,
            "stderr",
        )
        if raw_result.stderr_truncated:
            notes.append(f"stderr truncated to {self._settings.CODEX_CLI_OUTPUT_LIMIT_BYTES} bytes")
        redacted_error = bound_text(
            _redact_text(error),
            self._settings.CODEX_CLI_OUTPUT_LIMIT_BYTES,
            notes,
            "error",
        )

        evidence = DirectiveEvidence(
            workspace_id=_redact_text(directive_workspace_id),
            base_branch=_redact_evidence_field(
                request.base_branch,
                self._settings.CODEX_CLI_OUTPUT_LIMIT_BYTES,
            ),
            work_branch=_redact_evidence_field(
                request.work_branch,
                self._settings.CODEX_CLI_OUTPUT_LIMIT_BYTES,
            ),
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


def _guard_mode(settings: WorkerSettings, *, allowlisted: bool = False) -> str | None:
    if not (
        settings.CODEX_ASK_FOR_APPROVAL == "never"
        and settings.CODEX_STRICT_CONFIG
        and settings.CODEX_POLICY_HOOK_CONFIGURED
    ):
        return None
    if settings.CODEX_SANDBOX_MODE == "workspace-write":
        # The Profile's egress list replaced the old all-or-nothing `CODEX_NETWORK_ACCESS`
        # toggle (PRD issue 58): with a list, Codex's sandbox lets traffic out and the
        # attempt's egress proxy decides where it may go; without one, none leaves.
        network = "allowlisted" if allowlisted else "disabled"
        return (
            "sandbox=workspace-write; approval=never; strict-config; "
            f"network={network}; policy-hook=configured"
        )
    if settings.CODEX_SANDBOX_MODE == "danger-full-access":
        # Containerized posture: Codex's own OS sandbox cannot run inside the worker
        # pod (bwrap needs to create user namespaces, which the container runtime
        # denies — every exec/apply_patch failed with "bwrap: No permissions to
        # create a new namespace"). The pod is the isolation boundary instead:
        # dedicated container, PVC-scoped workspaces, cluster network policy.
        return (
            "sandbox=danger-full-access (pod-isolated); approval=never; strict-config; "
            "policy-hook=configured"
        )
    return None


def _codex_argv(settings: WorkerSettings, *, allowlisted: bool = False) -> list[str]:
    argv = [
        "codex",
        "exec",
        # JSONL event stream (research/29 §1.6) -- without it stdout is human prose and
        # `harness_usage.extract_codex_turn_usage` never finds a `turn.completed` line, so
        # a device-login Directive's harness Usage Record (PRD issue 31, 17 A9) is silent.
        "--json",
        "--sandbox",
        settings.CODEX_SANDBOX_MODE,
        # `codex exec` is non-interactive and rejects `--ask-for-approval`
        # (codex-cli 0.141.x exits 2 on it); the approval policy is expressed as a
        # config override instead.
        "--config",
        f"approval_policy={settings.CODEX_ASK_FOR_APPROVAL}",
        "--strict-config",
    ]
    if settings.CODEX_SANDBOX_MODE == "workspace-write":
        network_access = "true" if allowlisted else "false"
        argv += ["--config", f"sandbox_workspace_write.network_access={network_access}"]
    argv.append("-")
    return argv


def _refused_result(
    *,
    workspace_id: str,
    base_branch: str,
    work_branch: str,
    evidence_limit_bytes: int,
    guard_mode: str = "refused: missing sandbox/policy configuration",
    error: str = (
        "Codex sandbox/policy guard configuration is required before worker-local execution"
    ),
) -> DirectiveResult:
    command_hash = hash_command(["codex", "exec", "refused"])
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
