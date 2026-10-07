"""The granted MCP servers, in each CLI's native config shape (PRD issue 58, map 05).

Both runtimes consume MCP natively, so there is no shim: the Runner decides which servers
the Agent's Effective Grant lets it have (``agentic_runner.mcp``) and each runtime writes
exactly those into its own config -- Codex as ``mcp_servers`` tables, Claude Code as
``--mcp-config`` JSON. MCP cannot list a server while hiding its tools, so a server that is
not granted is simply never written.

Codex's copy is handed over as a ``--config mcp_servers=<inline table>`` override on the
``codex exec`` line rather than written into the Contract's ``config.toml``: two Agents of
one Contract share that file (ADR-0015 §4), and with N Directives in flight per Runner
(ADR-0013 §8) one Agent's servers would be read by the other's Directive.

That override does *not* replace the table: Codex merges it key by key with every config
layer it loads, so a ``[mcp_servers.*]`` in the harness root's ``config.toml`` or in a
Workspace ``.codex/config.toml`` still starts beside the Runner's (the LA-02 spike, then
re-run on the pinned codex-cli 0.141.0 for LA-19). ``codex_mcp_argv`` therefore closes both
layers as well:

- ``--ignore-user-config`` skips ``$CODEX_HOME/config.toml``; auth still reads
  ``CODEX_HOME``. The Runner writes nothing there, so nothing it relies on is lost.
- ``projects.<workspace>.trust_level="untrusted"`` keeps Codex from loading the Workspace's
  ``.codex/config.toml`` (and any nested one below it). ``--ignore-user-config`` alone is
  not enough: with a ``config.toml`` that does not parse -- which an earlier Directive, running
  as the same Contract uid, can write -- 0.141.0 falls back to loading the Workspace layer.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

__all__ = ["McpServerEntry", "claude_mcp_config", "codex_mcp_argv", "codex_mcp_override"]


@dataclass(frozen=True, slots=True)
class McpServerEntry:
    """One server as a CLI is told about it: a command it spawns, or a URL it calls.

    ``bearer_token_env`` names the environment variable holding the attempt's bearer --
    the *name*, so the token itself never lands in an argv or on disk. Never carries a
    credential: a server that needs one is Runner-hosted and reached by ``url``.
    """

    slug: str
    command: str | None = None
    args: tuple[str, ...] = ()
    url: str | None = None
    bearer_token_env: str | None = None
    required: bool = False


def _toml_string(value: str) -> str:
    # A JSON string is a valid TOML basic string: the same quote and the same escapes.
    return json.dumps(value)


def _codex_table(entry: McpServerEntry) -> str:
    fields: list[str] = []
    if entry.url is not None:
        fields.append(f"url={_toml_string(entry.url)}")
        if entry.bearer_token_env is not None:
            fields.append(f"bearer_token_env_var={_toml_string(entry.bearer_token_env)}")
    else:
        fields.append(f"command={_toml_string(entry.command or '')}")
        fields.append("args=[" + ",".join(_toml_string(arg) for arg in entry.args) + "]")
    if entry.required:
        # Codex aborts the run when a required server fails to initialise -- the
        # registry row's promise that the Directive is meaningless without it.
        fields.append("required=true")
    return f"{entry.slug}={{{','.join(fields)}}}"


def codex_mcp_override(entries: Iterable[McpServerEntry]) -> str:
    """``mcp_servers=<inline table>`` for ``codex exec --config``; ``{}`` when none."""

    return "mcp_servers={" + ",".join(_codex_table(entry) for entry in entries) + "}"


def codex_mcp_argv(entries: Iterable[McpServerEntry], workspace_path: Path) -> list[str]:
    """The ``codex exec`` arguments under which exactly ``entries`` start, and nothing else."""

    untrusted = f'projects={{{_toml_string(str(workspace_path))}={{trust_level="untrusted"}}}}'
    return [
        "--ignore-user-config",
        "--config",
        untrusted,
        "--config",
        codex_mcp_override(entries),
    ]


def claude_mcp_config(entries: Iterable[McpServerEntry]) -> str:
    """The inline JSON ``claude --mcp-config`` takes.

    A header value's ``${VAR}`` is expanded by Claude Code from its own environment, which
    is how the attempt's bearer reaches a Runner-hosted server without entering the argv.
    """

    servers: dict[str, dict[str, object]] = {}
    for entry in entries:
        if entry.url is not None:
            server: dict[str, object] = {"type": "http", "url": entry.url}
            if entry.bearer_token_env is not None:
                server["headers"] = {"Authorization": f"Bearer ${{{entry.bearer_token_env}}}"}
        else:
            server = {"type": "stdio", "command": entry.command or "", "args": list(entry.args)}
        servers[entry.slug] = server
    return json.dumps({"mcpServers": servers}, separators=(",", ":"))
