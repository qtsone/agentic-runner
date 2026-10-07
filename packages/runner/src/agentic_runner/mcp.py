"""MCP config assembly and Runner-hosted MCP servers (PRD issue 58; 05, 17 A4, ADR-0015 §3).

**Assembly** is the seam ``mcp:<server>`` lands with (ADR-0011 §3, §8): per Directive,
each registry row bound to the Work Record's Product is decided against the Agent's
Effective Grant (:func:`agentic_runner_contracts.grants.decide_mcp_server`) and only a
granted server reaches the CLI's config. ``confirm`` asks the owner *before* the server is
written -- MCP has no per-call seam inside the CLI, so config assembly is the narrowest
place a confirmation can bite.

**Placement** follows 17 A4. A stdio server with no credential is written as a command:
the CLI spawns it, so it runs as the Contract's uid, a grandchild of the Runner, with the
Directive's own credential-free environment. A row naming a Credential Reference is
**Runner-hosted**: :class:`RunnerHostedServer` spawns it as the Runner's own uid with the
value in its env, and exposes it to the CLI as Streamable HTTP on loopback behind the
attempt's bearer -- the Agent talks to the server and never holds what the server holds.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Final

from agentic_runner.tiny_http import bearer_matches, read_request, response_head, write_json
from agentic_runner.workers.mcp_config import McpServerEntry
from agentic_runner_contracts.grants import (
    Decision,
    GrantSnapshot,
    McpServerDecision,
    decide_mcp_server,
)
from agentic_runner_contracts.runtime_context import McpServerSpec

__all__ = [
    "MCP_ASSEMBLY_SOURCE",
    "McpPlan",
    "RunnerHostedServer",
    "Spawner",
    "entry_for",
    "plan_mcp",
]

# One Evidence Event per config assembly: granted, withheld and why (PRD issue 58).
MCP_ASSEMBLY_SOURCE: Final[str] = "runner.mcp_config_assembled"

# The one path a Runner-hosted server answers on, as Streamable HTTP's single endpoint.
MCP_PATH: Final[str] = "/mcp"

# What a Runner-hosted server inherits from the Runner besides its credential. A
# credential-bearing child must not also inherit the Runner's own environment (its
# Agent Token, a git token): only what a program needs to start.
_HOSTED_ENV_ALLOWLIST: Final[tuple[str, ...]] = ("PATH", "HOME", "LANG", "TZ")
_DEFAULT_CALL_TIMEOUT_SECONDS: Final[float] = 300.0

Spawner = Callable[..., Awaitable[asyncio.subprocess.Process]]


@dataclass(frozen=True)
class McpPlan:
    """What one Directive's config assembly decided, before anything is started."""

    granted: tuple[tuple[McpServerSpec, McpServerDecision], ...] = ()
    withheld: tuple[tuple[McpServerSpec, McpServerDecision, str], ...] = ()
    to_confirm: tuple[tuple[McpServerSpec, McpServerDecision], ...] = ()

    def evidence(self, *, egress_allow_list: Sequence[str] = ()) -> dict[str, object]:
        return {
            "event": "mcp.config_assembled",
            "granted": [
                {**decision.evidence(), "runner_hosted": spec.credential_reference is not None}
                for spec, decision in self.granted
            ],
            "withheld": [
                {**decision.evidence(), "withheld_because": why}
                for _, decision, why in self.withheld
            ],
            "egress_allow_list": list(egress_allow_list),
        }


def plan_mcp(
    snapshot: GrantSnapshot,
    servers: Sequence[McpServerSpec],
    *,
    consented: Collection[str] = (),
    declined: Collection[str] = (),
) -> McpPlan:
    """Decide every bound server against the Effective Grant.

    ``consented`` / ``declined`` are the ``mcp:<server>`` names the owner has already
    answered for this step; anything else that evaluates ``confirm`` is left in
    ``to_confirm`` for the caller to ask about before a single server is started.
    """

    granted: list[tuple[McpServerSpec, McpServerDecision]] = []
    withheld: list[tuple[McpServerSpec, McpServerDecision, str]] = []
    to_confirm: list[tuple[McpServerSpec, McpServerDecision]] = []
    for spec in servers:
        decision = decide_mcp_server(snapshot, server=spec.slug, tools=spec.tools)
        confirm = decision.decision is Decision.CONFIRM
        if decision.decision is Decision.ALLOW or (confirm and decision.verb in consented):
            granted.append((spec, decision))
        elif confirm and decision.verb in declined:
            withheld.append((spec, decision, "owner_declined"))
        elif confirm:
            to_confirm.append((spec, decision))
        else:
            withheld.append((spec, decision, "denied"))
    return McpPlan(granted=tuple(granted), withheld=tuple(withheld), to_confirm=tuple(to_confirm))


def entry_for(
    spec: McpServerSpec, *, hosted_url: str | None = None, bearer_env: str | None = None
) -> McpServerEntry:
    """The CLI's view of one granted server: its command, or the URL to call."""

    if hosted_url is not None:
        return McpServerEntry(
            slug=spec.slug, url=hosted_url, bearer_token_env=bearer_env, required=spec.required
        )
    if spec.transport == "streamable_http":
        return McpServerEntry(slug=spec.slug, url=str(spec.config["url"]), required=spec.required)
    return McpServerEntry(
        slug=spec.slug,
        command=str(spec.config["command"]),
        args=tuple(str(arg) for arg in _list(spec.config.get("args"))),
        required=spec.required,
    )


def _list(value: object) -> list[object]:
    return list(value) if isinstance(value, list) else []


class RunnerHostedServer:
    """One credentialed stdio server, spawned by the Runner and bridged to loopback HTTP.

    The Runner speaks stdio JSON-RPC to the child and answers the CLI's Streamable HTTP
    POSTs on ``127.0.0.1:<ephemeral>/mcp``, one request at a time, only for a caller
    presenting the attempt's bearer. The whole bridge dies with the attempt, which is
    what bounds the bearer (ADR-0011 §9).

    No SSE stream: a request is answered with one ``application/json`` body, a
    notification with ``202`` -- the subset of the transport both CLIs use for tool calls.
    """

    def __init__(
        self,
        spec: McpServerSpec,
        *,
        credential_env: Mapping[str, str],
        token: str,
        spawner: Spawner | None = None,
        host: str = "127.0.0.1",
        call_timeout_seconds: float = _DEFAULT_CALL_TIMEOUT_SECONDS,
    ) -> None:
        self.spec = spec
        self._credential_env = dict(credential_env)
        self._token = token
        self._spawner: Spawner = spawner or asyncio.create_subprocess_exec
        self._host = host
        self._timeout = call_timeout_seconds
        self._process: asyncio.subprocess.Process | None = None
        self._server: asyncio.Server | None = None
        self._lock = asyncio.Lock()
        self._port = 0

    @property
    def url(self) -> str:
        return f"http://{self._host}:{self._port}{MCP_PATH}"

    async def __aenter__(self) -> RunnerHostedServer:
        env = {name: os.environ[name] for name in _HOSTED_ENV_ALLOWLIST if name in os.environ}
        env.update(self._credential_env)
        # The Runner's own uid: no `user=` / `group=` here, deliberately. The credential
        # is the Runner's to hold, and the Contract's uid must not be able to read the
        # process that holds it (17 A4).
        self._process = await self._spawner(
            str(self.spec.config["command"]),
            *(str(arg) for arg in _list(self.spec.config.get("args"))),
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            self._server = await asyncio.start_server(self._serve, host=self._host, port=0)
        except BaseException:
            await self._stop_process()
            raise
        self._port = int(self._server.sockets[0].getsockname()[1])
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
        await self._stop_process()

    async def _stop_process(self) -> None:
        process = self._process
        if process is None or process.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        with contextlib.suppress(TimeoutError, ProcessLookupError):
            await asyncio.wait_for(process.wait(), timeout=5)

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request = await read_request(reader)
            if isinstance(request, tuple):
                await write_json(writer, request[0], request[1])
                return
            if not bearer_matches(request.authorization, self._token):
                await write_json(writer, 401, {"detail": "the attempt bearer is required"})
                return
            if request.target.split("?", 1)[0] != MCP_PATH:
                await write_json(writer, 404, {"detail": "not found"})
                return
            if request.method != "POST":
                await write_json(writer, 405, {"detail": "only POST is served"})
                return
            try:
                message = json.loads(request.body)
            except ValueError:
                await write_json(writer, 400, {"detail": "the body is not JSON"})
                return
            if not isinstance(message, dict):
                await write_json(writer, 400, {"detail": "one JSON-RPC message per request"})
                return
            reply = await self._call(message)
            if reply is None:
                writer.write(response_head(202, content_type="application/json", content_length=0))
                with contextlib.suppress(ConnectionError):
                    await writer.drain()
                return
            await write_json(writer, 200, reply)
        except TimeoutError:
            await write_json(writer, 504, {"detail": "the MCP server did not answer in time"})
        except (ConnectionError, RuntimeError):
            await write_json(writer, 502, {"detail": "the MCP server is not running"})
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _call(self, message: dict[str, Any]) -> dict[str, Any] | None:
        process = self._process
        if process is None or process.stdin is None or process.stdout is None:
            raise RuntimeError("the MCP server is not running")
        stdout = process.stdout
        expects_reply = "method" in message and "id" in message
        async with self._lock:
            process.stdin.write(json.dumps(message).encode() + b"\n")
            await process.stdin.drain()
            if not expects_reply:
                return None

            async def read_reply() -> dict[str, Any]:
                while True:
                    line = await stdout.readline()
                    if not line:
                        raise ConnectionError("the MCP server closed its stdout")
                    try:
                        candidate = json.loads(line)
                    except ValueError:
                        continue
                    # Its own notifications and log lines are not this call's answer.
                    if (
                        isinstance(candidate, dict)
                        and "method" not in candidate
                        and candidate.get("id") == message["id"]
                    ):
                        return candidate

            return await asyncio.wait_for(read_reply(), timeout=self._timeout)
