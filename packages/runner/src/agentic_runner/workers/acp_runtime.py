"""The ACP Agent Runtime: one Directive is one Agent Client Protocol turn (local-agents 12).

A third sibling of ``codex_runtime.py`` and ``claude_runtime.py`` behind the same port
(ADR-0006). Instead of a one-shot CLI it starts a pinned ACP bridge over stdio --
``codex-acp`` drives ``codex app-server``, ``claude-agent-acp`` drives ``claude`` -- under
the same Contract uid, harness root, ``DirectiveSandbox``, MCP servers and egress as the
per-CLI runtimes, and talks to it: ``initialize``, ``session/new`` in the Workspace with
exactly the servers ``mcp_config`` was handed, one ``session/prompt`` (ADR-0007: one
Directive, one turn), then close. What ACP adds is the seam the per-CLI runtimes lack:
every command the harness wants outside its own sandbox arrives as a
``session/request_permission`` the Runner answers from the command policy.

Facts this module stands on (the LA-02 spike, re-read against the pinned bridge sources on
2026-10-09):

- Neither bridge is ever sent ``authenticate``. codex-acp's ``api-key`` method writes the
  key into ``CODEX_HOME/auth.json``; claude-agent-acp's methods set a provider override.
  The harness authenticates on the login in its harness root (``subscription``) or on the
  attempt's LLM proxy pair (``api_key``), and on nothing else.
- codex-acp's default mode lets Codex's own reviewer approve escalations;
  ``INITIAL_AGENT_MODE=workspace-write`` puts them on the client.
- Codex reads the endpoint only from a ``model_provider`` in the ``config.toml`` it starts
  with (``OPENAI_BASE_URL`` is ignored, and the bridge's auth gate runs before any thread
  config applies). The proxy URL is per attempt and the Contract's harness root is shared
  by its concurrent Directives, so an ``api_key`` Codex Directive starts on a harness root
  of its own, written here and removed after the turn. Codex needs nothing else from the
  real root in that mode: no login, and the Skills directory is linked in.
- codex-acp trusts the Workspace unconditionally, so its ``.codex/config.toml`` loads, and
  it outranks the harness root: a ``model_provider``, ``notify``, ``sandbox_mode`` or
  ``mcp_servers`` there would take effect outside the permission seam, and by default the
  bridge even *drops* a sent server whose name a config file already uses. The per-CLI
  runtime's ``--config`` overrides outrank every file; here the provider is a file too. So
  filtering is switched off, a Directive whose Workspace has a ``.codex/config.toml`` at all
  is refused, and a harness-root ``config.toml`` may hold only ``_CODEX_HARNESS_ROOT_KEYS``
  (LA-19): nothing may load that the Runner did not choose.
- claude-agent-acp loads the Workspace's settings, hooks, ``.mcp.json`` and ``CLAUDE.md``
  unless ``session/new`` carries the three options in ``_CLAUDE_SESSION_OPTIONS``. Those
  keep the harness root's ``settings.json`` (the ``user`` source), which the Contract's uid
  can write: an allow rule, a hook, an ``env`` or an ``apiKeyHelper`` there would act
  outside the permission seam in every later Directive of that Contract. So it may hold
  only ``_CLAUDE_HARNESS_ROOT_KEYS`` (LA-12d item 2).
- A Claude permission request for an MCP tool names no command. It carries the serving
  server in ``_meta.claudeCode.mcpServer``, and ``source: "dynamic"`` is a server from the
  CLI's ``--mcp-config``: the ones ``session/new`` sent, the only ones ``strictMcpConfig``
  loads. A granted server's tool is allowed (LA-12d item 1).

A ``cli_kind`` with no pinned bridge (local-agents 18) runs the command its Agent Runtime
Profile names, and only on a Runner whose host pinned that kind's executable in
``ACP_PROFILE_CLI_KINDS`` (``gemini_cli=/usr/local/bin/gemini``). The Profile supplies the
arguments, never the program: an ``argv[0]`` that is not exactly the host-pinned path is
refused, or whoever edits the Profile would choose what runs on the host -- a shell, or a
binary the repository ships. None of the facts above are known for that harness. It gets
exactly a pinned bridge's isolation -- the Contract's uid,
``DirectiveSandbox``, the reserved env, the granted MCP servers, the egress proxy and the
permission seam -- with the harness root as ``XDG_CONFIG_HOME``, the one config-root
convention the Runner can assume of a harness it does not know.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shlex
import shutil
import tomllib
import uuid
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from agentic_runner.attempts import (
    PriorAttemptAliveError,
    recorded_group,
    refuse_if_prior_attempt_alive,
)
from agentic_runner.callback import CALLBACK_SOCKET_ENV, CALLBACK_TOKEN_ENV, call
from agentic_runner.llm_proxy import PROXY_API_KEY_ENV, PROXY_BASE_URL_ENV
from agentic_runner.usage_windows import claude_rate_limit_windows
from agentic_runner.workers._runtime_support import (
    apply_extra_env,
    bound_text,
    bound_text_tail,
    command_policy_refusal,
    hash_command,
    is_relative_to,
    redact_match,
    terminate_process_tree,
    workspace_id,
)
from agentic_runner.workers.agent_runtime import (
    AuthMode,
    DirectiveEvidence,
    DirectiveRequest,
    DirectiveResult,
    ModelUsage,
    PermissionFallback,
    RuntimeCapabilities,
)
from agentic_runner.workers.command_policy import CommandPolicy, evaluate_command_policy
from agentic_runner.workers.contract_isolation import DirectiveSandbox
from agentic_runner.workers.mcp_config import McpServerEntry
from agentic_runner.workers.settings import WorkerSettings
from agentic_runner_contracts.redaction import redact_secret_like_text
from agentic_runner_contracts.runner_registration import UsageWindow

__all__ = ["ACP_BRIDGES", "AcpBridge", "AcpRuntime", "profile_acp_executables"]


@dataclass(frozen=True, slots=True)
class AcpBridge:
    """One pinned bridge: the npm package the image ships, and the vendor CLI it drives."""

    package: str
    version: str
    executable: str
    # The variable the bridge reads the vendor binary's path from, so it runs the image's
    # own CLI rather than the copy in its npm dependency tree.
    harness_path_env: str
    harness_program: str
    config_root_env: str


# The Runner's, never a payload's, a repository's or a Profile's (local-agents 12 item 2).
# Bumped by hand with the image's install line, after reading the release for a change to
# the facts in the module docstring.
ACP_BRIDGES: Final[Mapping[str, AcpBridge]] = {
    "codex_cli": AcpBridge(
        package="@agentclientprotocol/codex-acp",
        version="2.1.1",
        executable="codex-acp",
        harness_path_env="CODEX_PATH",
        harness_program="codex",
        config_root_env="CODEX_HOME",
    ),
    "claude_code": AcpBridge(
        package="@agentclientprotocol/claude-agent-acp",
        version="0.88.0",
        executable="claude-agent-acp",
        harness_path_env="CLAUDE_CODE_EXECUTABLE",
        harness_program="claude",
        config_root_env="CLAUDE_CONFIG_DIR",
    ),
}

_CLI_KIND_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


def profile_acp_executables(settings: WorkerSettings) -> Mapping[str, str]:
    """Each kind this host opted in to run a Profile-named ACP command for, to the absolute
    path of the one executable it trusts for that kind (local-agents 18).

    A pinned kind, a malformed one, a kind named twice or a path that is not absolute is a
    ValueError, so a typo stops the Runner starting instead of leaving a kind silently
    unserved or served by whatever a relative name resolves to.
    """

    executables: dict[str, str] = {}
    for entry in (part.strip() for part in settings.ACP_PROFILE_CLI_KINDS.split(",")):
        if not entry:
            continue
        kind, separator, executable = (part.strip() for part in entry.partition("="))
        if kind in ACP_BRIDGES:
            raise ValueError(
                f"ACP_PROFILE_CLI_KINDS names {kind!r}, whose ACP bridge the Runner pins; "
                "a Profile never names the command for it"
            )
        if not _CLI_KIND_PATTERN.fullmatch(kind):
            raise ValueError(f"ACP_PROFILE_CLI_KINDS names {kind!r}, which is not a cli_kind")
        if not separator or not Path(executable).is_absolute():
            raise ValueError(
                f"ACP_PROFILE_CLI_KINDS must pin {kind!r} to an absolute executable path "
                f"({kind}=/path/to/executable)"
            )
        if kind in executables:
            raise ValueError(f"ACP_PROFILE_CLI_KINDS names {kind!r} twice")
        executables[kind] = executable
    return dict(sorted(executables.items()))


_ACP_PROTOCOL_VERSION: Final[int] = 1
# ACP frames are single JSON lines; a tool call carrying a file diff runs far past
# asyncio's 64 KiB default line limit.
_LINE_LIMIT_BYTES: Final[int] = 16 * 1024 * 1024
_CLOSE_GRACE_SECONDS: Final[float] = 5.0
_CODEX_PROVIDER_ID: Final[str] = "agentic_runner"
# What a `subscription` harness root may set: the model choice and the notices Codex
# records itself. Anything else -- a provider, `notify`, a sandbox or approval policy, a
# shell environment, MCP servers -- would be the Contract's own files steering the turn.
_CODEX_HARNESS_ROOT_KEYS: Final[frozenset[str]] = frozenset(
    {"model", "model_reasoning_effort", "model_reasoning_summary", "model_verbosity", "notice"}
)

# What a harness-root `settings.json` may set: the choices Claude Code writes there itself.
# `permissions`, `hooks`, `env`, `apiKeyHelper`, `enabledPlugins` and the rest would be the
# Contract's own file steering every later turn.
_CLAUDE_HARNESS_ROOT_KEYS: Final[frozenset[str]] = frozenset(
    {"$schema", "model", "effortLevel", "alwaysThinkingEnabled"}
)
# The `mcpServer.source` of a server the CLI got from `--mcp-config`, which is where the
# bridge puts `session/new`'s servers. A name alone is the config key as authored, so a
# server from any other source could reuse a granted slug.
_CLAUDE_SENT_MCP_SOURCE: Final[str] = "dynamic"

_CLAUDE_SESSION_OPTIONS: Final[Mapping[str, object]] = {
    # The harness root's own settings only: no Workspace settings, hooks or CLAUDE.md.
    "settingSources": ["user"],
    # Only the servers sent over ACP; never the Workspace's `.mcp.json`.
    "strictMcpConfig": True,
    # Bypass is never offered as a mode, whatever a settings file asks for.
    "allowDangerouslySkipPermissions": False,
}
# Edits inside the Workspace go through without a request, as `--dangerously-skip-
# permissions` let them before; every command still arrives as a permission request.
_CLAUDE_MODE: Final[str] = "acceptEdits"

# `evaluate_command_policy`'s verdict for a command that breaks no hard rule but is not on
# the allow-list: the case the policy has no answer for, which the Profile's
# `permission_fallback` decides (PRD decision 12). Every other refusal is the floor's.
_POLICY_SILENT_REASON: Final[str] = "command is not allowlisted"
_SHELL_WRAPPERS: Final[frozenset[str]] = frozenset({"sh", "bash", "zsh"})
_SHELL_SYNTAX: Final[re.Pattern[str]] = re.compile(r"[;&|<>`$()\n\\*?{}\[\]]")

_SECRET_VALUE_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(
        r"(?i)((?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|password|credential)\s*[=:]\s*)([^\s,}]+)"
    ),
    re.compile(
        r"(?i)((?:\"(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|password|credential)\"\s*:\s*\"))([^\"]+)(\")"
    ),
    re.compile(r"(?i)(authorization:\s*bearer\s+)([^\s]+)"),
    re.compile(r"(?i)(bearer\s+)([A-Za-z0-9._~+/-]{8,})"),
    re.compile(r"(?i)(sk-[A-Za-z0-9_-]{8,})"),
    re.compile(r"(eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})"),
    re.compile(r"(?i)(?:~|/[^\s]*)/\.(?:codex|claude)(?:/[^\s-]+)?"),
    re.compile(r"(?is)-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----"),
)


class AcpError(RuntimeError):
    """A JSON-RPC error the bridge answered with; the text reaches issue 08's classifier."""

    def __init__(self, code: int, message: str, data: object = None) -> None:
        detail = "" if data is None else f" {json.dumps(data, sort_keys=True, default=str)}"
        super().__init__(f"ACP error {code}: {message}{detail}")
        self.code = code


class _Verdict(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    SILENT = "silent"


Ask = Callable[[str, Mapping[str, str]], Awaitable[bool]]


async def ask_over_callback(text: str, env: Mapping[str, str]) -> bool:
    """Raise a Question through the attempt's own ``work.ask`` callback (PRD issue 60).

    The same route ``agentic-runner ask`` takes from inside the harness, so the one
    Question per Directive rule, the Grant and the redaction all hold unchanged; the
    Work Record then holds once the Directive ends, and the answer reaches the next one.
    """

    socket_path, token = env.get(CALLBACK_SOCKET_ENV), env.get(CALLBACK_TOKEN_ENV)
    if not socket_path or not token:
        return False
    try:
        answer = await asyncio.to_thread(
            call, socket_path=socket_path, token=token, path="/v0/ask", payload={"text": text}
        )
    except Exception:  # noqa: BLE001 - a Question that cannot be raised is a denial
        return False
    return bool(answer.get("accepted"))


@dataclass
class _Turn:
    """What one turn accumulated while it ran."""

    output: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    held: bool = False
    usage_windows: tuple[UsageWindow, ...] = ()


class AcpRuntime:
    """An Agent Runtime that serves one ``cli_kind`` through its pinned ACP bridge, or, for
    a kind with none, through the command the Directive's Profile names."""

    auth_modes = frozenset({AuthMode.API_KEY, AuthMode.SUBSCRIPTION})
    # Never a key of its own: an `api_key` Directive runs on the attempt's proxy pair.
    host_api_key = False

    def __init__(
        self,
        *,
        cli_kind: str,
        settings: WorkerSettings,
        bridge_argv: Sequence[str] | None = None,
        ask: Ask = ask_over_callback,
    ) -> None:
        self._cli_kind = cli_kind
        self._bridge = ACP_BRIDGES.get(cli_kind)
        self._profile_executable = (
            None if self._bridge is not None else profile_acp_executables(settings).get(cli_kind)
        )
        self._settings = settings
        # Tests drive a fake ACP agent through this; production runs the pinned bridge.
        self._bridge_argv = list(bridge_argv) if bridge_argv is not None else None
        self._ask = ask

    @property
    def _is_codex(self) -> bool:
        return self._cli_kind == "codex_cli"

    @property
    def _is_claude(self) -> bool:
        return self._cli_kind == "claude_code"

    @property
    def _executable(self) -> str:
        return self._bridge.executable if self._bridge is not None else self._cli_kind

    def acp_command_refusal(self, acp_command: Sequence[str]) -> str | None:
        """Why this runtime will not run the Profile-named ``acp_command``, or None.

        None for a pinned kind: its bridge runs and the Profile's command is never read
        (the activity refuses one sent for it). For any other kind the host's pinned
        executable must be exactly ``argv[0]``; the Profile chooses only the arguments.
        """

        if self._bridge is not None:
            return None
        if not acp_command:
            return f"the Agent Runtime Profile names no ACP command for {self._cli_kind}"
        if self._profile_executable is None:
            return f"this Runner's host pinned no ACP executable for {self._cli_kind}"
        if not Path(acp_command[0]).is_absolute():
            return f"the Profile's ACP command for {self._cli_kind} is not an absolute path"
        if acp_command[0] != self._profile_executable:
            return (
                f"the Profile's ACP command for {self._cli_kind} is not the executable this "
                "Runner's host pinned for it"
            )
        return None

    @property
    def _timeout_seconds(self) -> int:
        if self._is_codex:
            return self._settings.CODEX_CLI_TIMEOUT_SECONDS
        return self._settings.CLAUDE_CLI_TIMEOUT_SECONDS

    @property
    def _limit_bytes(self) -> int:
        if self._is_codex:
            return self._settings.CODEX_CLI_OUTPUT_LIMIT_BYTES
        return self._settings.CLAUDE_CLI_OUTPUT_LIMIT_BYTES

    def capabilities(self) -> RuntimeCapabilities:
        # Every escalation is a `session/request_permission` answered from the command
        # policy; Codex additionally starts in `workspace-write` so its own reviewer never
        # approves one. There is no unguarded configuration to report as refused.
        return RuntimeCapabilities(
            auth_modes=self.auth_modes,
            permission_mode=(
                "acp;mode=workspace-write;permission=policy"
                if self._is_codex
                else "acp;permission=policy"
            ),
        )

    async def execute_directive(self, request: DirectiveRequest) -> DirectiveResult:
        workspace_path = request.workspace_path.resolve(strict=False)
        workspace_root = self._settings.WORKSPACE_ROOT.resolve(strict=False)
        if not is_relative_to(workspace_path, workspace_root):
            raise ValueError("workspace_path must be under WORKSPACE_ROOT")
        directive_workspace_id = workspace_id(workspace_path, workspace_root)

        def refuse(guard_mode: str, error: str) -> DirectiveResult:
            return self._refused(request, directive_workspace_id, guard_mode, error)

        extra_env = dict(request.extra_env)
        # The name `llm_proxy.attempt_env` gave this kind its endpoint under.
        proxy_url = extra_env.get(
            "ANTHROPIC_BASE_URL" if self._cli_kind.startswith("claude") else PROXY_BASE_URL_ENV,
            "",
        )
        if request.auth_mode == AuthMode.API_KEY and not proxy_url:
            # Without the proxy pair the only credential left for the harness is a login in
            # its harness root, which is exactly what this mode must not use.
            return refuse(
                "refused: api_key mode without an LLM proxy endpoint",
                f"an api_key {self._cli_kind} Directive needs the attempt's LLM proxy endpoint",
            )

        sandbox = request.sandbox
        harness_root = self._harness_root(sandbox)
        if self._is_codex:
            config_refusal = _codex_config_refusal(
                workspace_path,
                harness_root if request.auth_mode == AuthMode.SUBSCRIPTION else None,
            )
            if config_refusal:
                return refuse("refused: Codex config file", config_refusal)
        if self._is_claude and harness_root is not None:
            settings_refusal = _claude_settings_refusal(harness_root)
            if settings_refusal:
                return refuse("refused: Claude settings file", settings_refusal)

        # The activity refuses this first, with Evidence; checked again where it runs.
        command_refusal = self.acp_command_refusal(request.acp_command)
        if command_refusal is not None:
            return refuse("refused: Profile ACP command", command_refusal)
        argv = self._argv(request)
        command_hash = hash_command(argv)
        floor_refusal = command_policy_refusal(
            argv=argv,
            program=Path(argv[0]).name,
            workspace_path=workspace_path,
            workspace_root=workspace_root,
            base_branch=request.base_branch,
            work_branch=request.work_branch,
        )
        if floor_refusal is not None:
            return refuse(f"refused: command policy ({floor_refusal})", floor_refusal)

        if self._bridge is not None:
            runs = f"bridge={self._bridge.package}@{self._bridge.version}"
        else:
            # The executable's name and nothing of its arguments here; the note below
            # carries them, scrubbed (local-agents 18 item 4).
            command = redact_secret_like_text(Path(argv[0]).name)
            runs = f"cli_kind={self._cli_kind}; command={command}"
        guard_mode = (
            f"runtime=acp; {runs}; auth={request.auth_mode.value}; "
            f"permission=command-policy; fallback={request.permission_fallback.value}"
        )
        turn = _Turn()
        if self._bridge is None:
            turn.notes.append(f"profile ACP command: {redact_secret_like_text(shlex.join(argv))}")
        timed_out = False
        async with contextlib.AsyncExitStack() as stack:
            env = self._env(request, harness_root)
            if self._is_codex and request.auth_mode == AuthMode.API_KEY:
                assert harness_root is not None  # Codex always has one: see _harness_root
                env[ACP_BRIDGES["codex_cli"].config_root_env] = str(
                    stack.enter_context(_attempt_codex_home(proxy_url, harness_root, sandbox))
                )
            apply_extra_env(env, request.extra_env)
            try:
                stop_reason, model_usage, stderr, error = await self._run(
                    argv, request, workspace_path, env, extra_env, turn
                )
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                timed_out = True
                stop_reason, model_usage, stderr = "", (), ""
                error = f"{self._executable} timed out after {self._timeout_seconds}s"
            except OSError as exception:
                return self._result(
                    request,
                    directive_workspace_id,
                    command_hash,
                    guard_mode,
                    turn,
                    exit_code=127,
                    stderr="",
                    error=str(exception),
                )

        if timed_out:
            exit_code = 124
        elif error:
            exit_code = 1
        elif stop_reason == "end_turn" or (stop_reason == "cancelled" and turn.held):
            exit_code = 0
        else:
            exit_code = 1
            error = f"turn ended: {stop_reason or 'no stop reason'}"
        return self._result(
            request,
            directive_workspace_id,
            command_hash,
            guard_mode,
            turn,
            exit_code=exit_code,
            stderr=stderr,
            error=error,
            model_usage=model_usage,
        )

    def _argv(self, request: DirectiveRequest) -> list[str]:
        if self._bridge is None:
            # Only for a kind with no pinned bridge: for a pinned one the activity refuses a
            # Profile-named command before it gets here, and this never reads it.
            return list(request.acp_command)
        argv = list(self._bridge_argv) if self._bridge_argv else [self._bridge.executable]
        if self._is_claude and request.auth_mode == AuthMode.API_KEY:
            # The bridge's own guard: refuse any turn that would bill a claude.ai login.
            argv.append("--hide-claude-auth")
        return argv

    def _harness_root(self, sandbox: DirectiveSandbox | None) -> Path | None:
        if sandbox is not None:
            return sandbox.harness_config_dir
        # As the per-CLI runtimes without a sandbox: Codex on the Runner's CODEX_HOME, Claude
        # on its own default -- never pointed at the Codex home.
        return self._settings.CODEX_HOME if self._is_codex else None

    def _env(self, request: DirectiveRequest, harness_root: Path | None) -> dict[str, str]:
        path = os.environ.get("PATH") or os.defpath
        env = {"PATH": path}
        if harness_root is not None:
            config_root_env = (
                self._bridge.config_root_env if self._bridge is not None else "XDG_CONFIG_HOME"
            )
            env[config_root_env] = str(harness_root)
        if request.sandbox is not None:
            env["HOME"] = str(request.sandbox.home_dir)
            env["TMPDIR"] = str(request.sandbox.tmp_dir)
        if self._bridge is not None:
            program = self._bridge.harness_program
            env[self._bridge.harness_path_env] = shutil.which(program, path=path) or program
        if self._is_codex:
            env["INITIAL_AGENT_MODE"] = "workspace-write"
            env["NO_BROWSER"] = "1"
            # Otherwise the bridge drops a granted server whose name a config file reuses.
            env["DISABLE_MCP_CONFIG_FILTERING"] = "true"
        return env

    async def _run(
        self,
        argv: list[str],
        request: DirectiveRequest,
        workspace_path: Path,
        env: dict[str, str],
        extra_env: Mapping[str, str],
        turn: _Turn,
    ) -> tuple[str, tuple[ModelUsage, ...], str, str]:
        refuse_if_prior_attempt_alive()
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=workspace_path,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            limit=_LINE_LIMIT_BYTES,
            **({} if request.sandbox is None else request.sandbox.spawn_kwargs()),
        )
        stderr_task = asyncio.create_task(_read_tail(process.stderr, self._limit_bytes))
        connection = _Connection(process, self._handler(request, workspace_path, env, turn))
        try:
            with recorded_group(process.pid):
                try:
                    stop_reason, model_usage, error = await asyncio.wait_for(
                        self._turn(connection, request, workspace_path, extra_env),
                        timeout=self._timeout_seconds,
                    )
                finally:
                    await connection.close()
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(process.wait(), timeout=_CLOSE_GRACE_SECONDS)
                    await terminate_process_tree(process)
        except PriorAttemptAliveError:
            await terminate_process_tree(process)
            raise
        stderr = await stderr_task
        return stop_reason, model_usage, stderr, error

    async def _turn(
        self,
        connection: _Connection,
        request: DirectiveRequest,
        workspace_path: Path,
        extra_env: Mapping[str, str],
    ) -> tuple[str, tuple[ModelUsage, ...], str]:
        try:
            await connection.request(
                "initialize",
                {
                    "protocolVersion": _ACP_PROTOCOL_VERSION,
                    # No file system or terminal of ours to lend: the harness works in the
                    # Workspace with its own tools, and every command still asks.
                    "clientCapabilities": {
                        "fs": {"readTextFile": False, "writeTextFile": False},
                        "terminal": False,
                    },
                },
            )
            session_params: dict[str, Any] = {
                "cwd": str(workspace_path),
                "mcpServers": acp_mcp_servers(request.mcp_servers or (), extra_env),
            }
            if self._is_claude:
                session_params["_meta"] = {
                    "claudeCode": {
                        "options": {**_CLAUDE_SESSION_OPTIONS, "model": self._settings.CLAUDE_MODEL}
                    }
                }
            session = await connection.request("session/new", session_params)
            session_id = str(session.get("sessionId") or "")
            if self._is_claude and _offers_mode(session, _CLAUDE_MODE):
                await connection.request(
                    "session/set_mode", {"sessionId": session_id, "modeId": _CLAUDE_MODE}
                )
            response = await connection.request(
                "session/prompt",
                {"sessionId": session_id, "prompt": [{"type": "text", "text": request.prompt}]},
            )
        except AcpError as exception:
            return "", (), str(exception)
        except ConnectionError as exception:
            return "", (), str(exception) or f"{self._executable} closed its stdout"
        return str(response.get("stopReason") or ""), _model_usage(response), ""

    def _handler(
        self,
        request: DirectiveRequest,
        workspace_path: Path,
        env: Mapping[str, str],
        turn: _Turn,
    ) -> Callable[[str, Mapping[str, Any]], Awaitable[Mapping[str, Any] | None]]:
        async def handle(method: str, params: Mapping[str, Any]) -> Mapping[str, Any] | None:
            if method == "session/update":
                _collect_update(params, turn)
                return None
            if method == "session/request_permission":
                return await self._answer_permission(params, request, workspace_path, env, turn)
            raise AcpError(-32601, f"method not found: {method}")

        return handle

    async def _answer_permission(
        self,
        params: Mapping[str, Any],
        request: DirectiveRequest,
        workspace_path: Path,
        env: Mapping[str, str],
        turn: _Turn,
    ) -> Mapping[str, Any]:
        tool_call = params.get("toolCall") or {}
        title = str(tool_call.get("title") or "") if isinstance(tool_call, Mapping) else ""
        mcp_server = (
            _granted_mcp_server(tool_call, request.mcp_servers or ()) if self._is_claude else None
        )
        if mcp_server is not None:
            # Checked first: an MCP tool's arguments are not a command, even one named so.
            described = _redact(f"{title} (MCP server {mcp_server})")
            verdict, reason = _Verdict.ALLOW, "a granted MCP server's tool"
        else:
            raw_input = tool_call.get("rawInput") if isinstance(tool_call, Mapping) else None
            argv, cwd = _requested_command(raw_input, workspace_path)
            described = _redact(shlex.join(argv) if argv else title or "an action")
            # The Directive's own Workspace, not WORKSPACE_ROOT: without a Contract uid
            # nothing else keeps an allowlisted command out of another Work Record's
            # Workspace.
            verdict, reason = _verdict(argv, cwd, request, workspace_path)
        options = params.get("options") or []
        if verdict == _Verdict.ALLOW:
            turn.notes.append(f"permission allowed: {described}")
            return _select(options, "allow_once")
        if verdict == _Verdict.SILENT and request.permission_fallback == PermissionFallback.HOLD:
            asked = await self._ask(
                f"The Agent asked to run `{described}`, which this Profile's command policy "
                "does not allow on its own. Reply with what it should do instead, or say it "
                "may go ahead.",
                env,
            )
            if asked:
                turn.held = True
                turn.notes.append(f"permission held for a person: {described}")
                return _select(options, "reject_once")
            reason = "the Question could not be raised"
        turn.notes.append(f"permission denied: {described} ({reason})")
        # `reject_once`, never `reject_always`/`allow_always`: nothing outlives the turn.
        return _select(options, "reject_once")

    def _refused(
        self,
        request: DirectiveRequest,
        directive_workspace_id: str,
        guard_mode: str,
        error: str,
    ) -> DirectiveResult:
        command_hash = hash_command([self._executable, "refused"])
        return DirectiveResult(
            exit_code=126,
            stdout="",
            stderr="",
            error=error,
            command_hash=command_hash,
            evidence=self._evidence(request, directive_workspace_id, command_hash, guard_mode, []),
        )

    def _result(
        self,
        request: DirectiveRequest,
        directive_workspace_id: str,
        command_hash: str,
        guard_mode: str,
        turn: _Turn,
        *,
        exit_code: int,
        stderr: str,
        error: str,
        model_usage: tuple[ModelUsage, ...] = (),
    ) -> DirectiveResult:
        limit = self._limit_bytes
        notes = list(turn.notes)
        stdout = bound_text_tail(_redact("".join(turn.output)), limit, notes, "stdout")
        return DirectiveResult(
            exit_code=exit_code,
            stdout=stdout,
            stderr=bound_text_tail(_redact(stderr), limit, notes, "stderr"),
            error=bound_text(_redact(error), limit, notes, "error"),
            command_hash=command_hash,
            evidence=self._evidence(
                request, directive_workspace_id, command_hash, guard_mode, notes
            ),
            model_usage=model_usage,
            usage_windows=turn.usage_windows,
        )

    def _evidence(
        self,
        request: DirectiveRequest,
        directive_workspace_id: str,
        command_hash: str,
        guard_mode: str,
        notes: list[str],
    ) -> DirectiveEvidence:
        limit = self._limit_bytes
        return DirectiveEvidence(
            workspace_id=_redact(directive_workspace_id),
            base_branch=bound_text(_redact(request.base_branch), limit, [], "evidence"),
            work_branch=bound_text(_redact(request.work_branch), limit, [], "evidence"),
            command_hash=command_hash,
            guard_mode=guard_mode,
            notes=notes,
        )


class _Connection:
    """JSON-RPC 2.0 over the bridge's stdio, one JSON object per line (the ACP transport)."""

    def __init__(
        self,
        process: asyncio.subprocess.Process,
        handle: Callable[[str, Mapping[str, Any]], Awaitable[Mapping[str, Any] | None]],
    ) -> None:
        assert process.stdin is not None and process.stdout is not None
        self._stdin = process.stdin
        self._stdout = process.stdout
        self._handle = handle
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._reader = asyncio.create_task(self._read())
        self._handlers: set[asyncio.Task[None]] = set()

    async def request(self, method: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
        self._next_id += 1
        request_id = self._next_id
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        await self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        result = await future
        return result if isinstance(result, Mapping) else {}

    async def close(self) -> None:
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            self._stdin.close()
        self._reader.cancel()
        for task in self._handlers:
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._reader

    async def _send(self, message: Mapping[str, Any]) -> None:
        self._stdin.write(json.dumps(message, separators=(",", ":")).encode() + b"\n")
        await self._stdin.drain()

    async def _read(self) -> None:
        try:
            while line := await self._stdout.readline():
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if isinstance(message, dict):
                    self._dispatch(message)
        finally:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(ConnectionError("the ACP bridge closed its stdout"))

    def _dispatch(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        if not isinstance(method, str):
            future = self._pending.pop(message.get("id"), None)  # type: ignore[arg-type]
            if future is None or future.done():
                return
            error = message.get("error")
            if isinstance(error, dict):
                future.set_exception(
                    AcpError(
                        int(error.get("code") or 0), str(error.get("message")), error.get("data")
                    )
                )
            else:
                future.set_result(message.get("result"))
            return
        params = message.get("params")
        task = asyncio.create_task(
            self._serve(message.get("id"), method, params if isinstance(params, dict) else {})
        )
        self._handlers.add(task)
        task.add_done_callback(self._handlers.discard)

    async def _serve(self, request_id: object, method: str, params: Mapping[str, Any]) -> None:
        try:
            result = await self._handle(method, params)
            reply: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "result": result or {}}
        except AcpError as error:
            reply = {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": error.code, "message": str(error)},
            }
        except Exception as error:  # noqa: BLE001 - an unanswered request would hang the turn
            reply = {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32603, "message": error.__class__.__name__},
            }
        if request_id is not None:
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                await self._send(reply)


def acp_mcp_servers(
    entries: Sequence[McpServerEntry], env: Mapping[str, str]
) -> list[dict[str, Any]]:
    """The granted servers in ACP's ``session/new`` shape, and nothing else.

    A URL server's bearer is read from the attempt's env by the name the entry carries:
    it travels over the bridge's stdin, never an argv or a file.
    """

    servers: list[dict[str, Any]] = []
    for entry in entries:
        if entry.url is not None:
            token = env.get(entry.bearer_token_env or "")
            headers = [{"name": "Authorization", "value": f"Bearer {token}"}] if token else []
            servers.append(
                {"type": "http", "name": entry.slug, "url": entry.url, "headers": headers}
            )
        else:
            servers.append(
                {
                    "name": entry.slug,
                    "command": entry.command or "",
                    "args": list(entry.args),
                    "env": [],
                }
            )
    return servers


def _codex_config_refusal(workspace_path: Path, harness_root: Path | None) -> str | None:
    """Why a config file Codex would load refuses the Directive, or None.

    Both files outrank nothing the Runner sets on argv here -- the proxy provider is itself a
    file layer -- so any key in them reaches Codex. The Workspace's may not exist at all; the
    harness root's may hold only ``_CODEX_HARNESS_ROOT_KEYS``. The names, not the paths:
    the refusal text leaves for the control plane.
    """

    if (workspace_path / ".codex" / "config.toml").exists():
        return "the Workspace's .codex/config.toml is not loaded by the Runner; remove it"
    if harness_root is None:
        return None
    try:
        text = (harness_root / "config.toml").read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        return "the harness root's config.toml cannot be read"
    try:
        config = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        # Unreadable to us is not unreadable to Codex: refuse rather than guess.
        return "the harness root's config.toml does not parse"
    unexpected = sorted(set(config) - _CODEX_HARNESS_ROOT_KEYS)
    if unexpected:
        return f"the harness root's config.toml sets {', '.join(unexpected)}; remove them"
    return None


def _claude_settings_refusal(harness_root: Path) -> str | None:
    """Why the harness root's ``settings.json`` refuses a Claude Directive, or None.

    As ``_codex_config_refusal``: the key names, never the path, reach the control plane.
    """

    try:
        text = (harness_root / "settings.json").read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        return "the harness root's settings.json cannot be read"
    try:
        settings = json.loads(text)
    except ValueError:
        return "the harness root's settings.json does not parse"
    if not isinstance(settings, dict):
        return "the harness root's settings.json is not an object"
    unexpected = sorted(set(settings) - _CLAUDE_HARNESS_ROOT_KEYS)
    if unexpected:
        return f"the harness root's settings.json sets {', '.join(unexpected)}; remove them"
    return None


def _granted_mcp_server(tool_call: object, granted: Sequence[McpServerEntry]) -> str | None:
    """The granted server a Claude permission request's MCP tool is served by, or None."""

    if not isinstance(tool_call, Mapping):
        return None
    meta = tool_call.get("_meta")
    claude = meta.get("claudeCode") if isinstance(meta, Mapping) else None
    server = claude.get("mcpServer") if isinstance(claude, Mapping) else None
    if not isinstance(server, Mapping) or server.get("source") != _CLAUDE_SENT_MCP_SOURCE:
        return None
    name = server.get("name")
    return name if isinstance(name, str) and name in {e.slug for e in granted} else None


@contextlib.contextmanager
def _attempt_codex_home(
    proxy_url: str, harness_root: Path, sandbox: DirectiveSandbox | None
) -> Iterator[Path]:
    """A harness root for one ``api_key`` Codex Directive: the proxy provider, no login.

    ``cli_auth_credentials_store = "ephemeral"`` keeps Codex from ever opening an
    ``auth.json`` here. Owned by the Contract's uid like the real root, and removed with
    whatever the turn wrote into it.
    """

    parent = harness_root.parent if sandbox is None else sandbox.tmp_dir
    home = parent / f"codex-acp-{uuid.uuid4().hex}"
    home.mkdir(mode=0o700, parents=True)
    provider = (
        f"name = {json.dumps(_CODEX_PROVIDER_ID)}\n"
        f"base_url = {json.dumps(proxy_url)}\n"
        f"env_key = {json.dumps(PROXY_API_KEY_ENV)}\n"
        'wire_api = "responses"\n'
    )
    (home / "config.toml").write_text(
        f"model_provider = {json.dumps(_CODEX_PROVIDER_ID)}\n"
        'cli_auth_credentials_store = "ephemeral"\n\n'
        f"[model_providers.{_CODEX_PROVIDER_ID}]\n{provider}",
        encoding="utf-8",
    )
    skills = harness_root / "skills"
    if skills.is_dir():
        (home / "skills").symlink_to(skills, target_is_directory=True)
    if sandbox is not None and sandbox.uid is not None:
        for path in (home, home / "config.toml"):
            os.chown(path, sandbox.uid, sandbox.gid if sandbox.gid is not None else sandbox.uid)
    try:
        yield home
    finally:
        shutil.rmtree(home, ignore_errors=True)


def _requested_command(raw_input: object, workspace_path: Path) -> tuple[list[str] | None, Path]:
    """The argv a permission request asks to run, unwrapped from ``bash -lc`` when the
    script is one plain command; None when it names no command or real shell syntax."""

    if not isinstance(raw_input, Mapping):
        return None, workspace_path
    cwd_value = raw_input.get("cwd")
    cwd = Path(cwd_value) if isinstance(cwd_value, str) and cwd_value else workspace_path
    command = raw_input.get("command")
    argv: list[str] | None
    if isinstance(command, str):
        argv = None if _SHELL_SYNTAX.search(command) else _split(command)
    elif isinstance(command, list) and all(isinstance(part, str) for part in command):
        argv = list(command)
    else:
        return None, cwd
    if (
        argv
        and len(argv) == 3
        and Path(argv[0]).name in _SHELL_WRAPPERS
        and argv[1] in {"-c", "-lc"}
    ):
        script = argv[2]
        argv = None if _SHELL_SYNTAX.search(script) else _split(script)
    return argv or None, cwd


def _split(command: str) -> list[str] | None:
    try:
        return shlex.split(command)
    except ValueError:
        return None


def _verdict(
    argv: list[str] | None, cwd: Path, request: DirectiveRequest, workspace_path: Path
) -> tuple[_Verdict, str]:
    if argv is None:
        return _Verdict.SILENT, "not a command the policy can read"
    decision = evaluate_command_policy(
        argv=argv,
        cwd=cwd,
        workspace_root=workspace_path,
        base_branch=request.base_branch,
        work_branch=request.work_branch,
        policy=CommandPolicy(),
    )
    if decision.allowed:
        return _Verdict.ALLOW, decision.reason
    if decision.reason == _POLICY_SILENT_REASON:
        return _Verdict.SILENT, decision.reason
    return _Verdict.DENY, decision.reason


def _select(options: object, kind: str) -> Mapping[str, Any]:
    """The option of exactly ``kind``; a request offering none of that kind is cancelled."""

    for option in options if isinstance(options, list) else []:
        if isinstance(option, Mapping) and option.get("kind") == kind and option.get("optionId"):
            return {"outcome": {"outcome": "selected", "optionId": option["optionId"]}}
    return {"outcome": {"outcome": "cancelled"}}


def _offers_mode(session: Mapping[str, Any], mode_id: str) -> bool:
    modes = session.get("modes")
    available = modes.get("availableModes") if isinstance(modes, Mapping) else None
    return any(
        isinstance(mode, Mapping) and mode.get("id") == mode_id
        for mode in (available if isinstance(available, list) else [])
    )


def _collect_update(params: Mapping[str, Any], turn: _Turn) -> None:
    update = params.get("update")
    if not isinstance(update, Mapping):
        return
    if update.get("sessionUpdate") == "usage_update":
        # claude-agent-acp's relay of the CLI's rate-limit event; the latest one wins.
        meta = update.get("_meta")
        if isinstance(meta, Mapping) and (
            windows := claude_rate_limit_windows(meta.get("_claude/rateLimit"))
        ):
            turn.usage_windows = tuple(windows)
        return
    if update.get("sessionUpdate") != "agent_message_chunk":
        return
    content = update.get("content")
    if isinstance(content, Mapping) and content.get("type") == "text":
        turn.output.append(str(content.get("text") or ""))


def _model_usage(response: Mapping[str, Any]) -> tuple[ModelUsage, ...]:
    """``_meta.quota.model_usage``: per model, subagents and side calls included."""

    meta = response.get("_meta")
    quota = meta.get("quota") if isinstance(meta, Mapping) else None
    rows = quota.get("model_usage") if isinstance(quota, Mapping) else None
    usage: list[ModelUsage] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, Mapping) or not isinstance(row.get("model"), str):
            continue
        count = row.get("token_count")
        if not isinstance(count, Mapping):
            continue
        usage.append(
            ModelUsage(
                model=row["model"],
                input_tokens=_count(count, "inputTokens"),
                output_tokens=_count(count, "outputTokens"),
                cached_read_tokens=_count(count, "cachedInputTokens"),
                cached_write_tokens=_count(count, "cachedWriteTokens"),
                reasoning_output_tokens=_count(count, "reasoningOutputTokens"),
            )
        )
    return tuple(usage)


def _count(values: Mapping[str, Any], key: str) -> int:
    value = values.get(key)
    return int(value) if isinstance(value, int | float) and not isinstance(value, bool) else 0


async def _read_tail(stream: asyncio.StreamReader | None, limit_bytes: int) -> str:
    if stream is None:
        return ""
    tail = bytearray()
    while chunk := await stream.read(4096):
        tail.extend(chunk)
        if len(tail) > limit_bytes:
            del tail[: len(tail) - limit_bytes]
    return tail.decode(errors="replace")


def _redact(value: str) -> str:
    for pattern in _SECRET_VALUE_PATTERNS:
        value = pattern.sub(redact_match, value)
    return value
