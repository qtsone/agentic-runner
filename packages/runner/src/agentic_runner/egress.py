"""Scoped egress for one Directive (PRD issue 58, map 06 §4, ticket 05's open item).

``CODEX_NETWORK_ACCESS`` was all-or-nothing: a Directive either reached the whole internet
or nothing, and a VPC-only Streamable HTTP MCP server needs neither. The Agent Runtime
Profile's ``network_policy`` now names the destinations a Directive may reach; the Runner
adds the ones it knows the Directive needs (the git remote, each granted HTTP MCP
server), and this forward proxy on loopback is where the list is enforced: every
Directive under a Profile allow-list is handed ``HTTPS_PROXY`` / ``HTTP_PROXY`` /
``ALL_PROXY`` naming it, with the attempt's bearer as the proxy credential so another
Contract's uid on the same loopback cannot borrow this attempt's list.

What this is *not* is a kernel boundary. A process that ignores the proxy variables and
opens a raw socket is stopped only by what the host adds underneath (a pod
NetworkPolicy); a workstation adds nothing, which is why it reports its posture as
``unenforced`` in the heartbeat rather than claiming the list binds.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hmac
from collections.abc import Awaitable, Callable, Iterable
from fnmatch import fnmatchcase
from types import TracebackType
from typing import Final
from urllib.parse import urlsplit

from agentic_runner.tiny_http import HttpRequest, read_request, response_head

__all__ = [
    "EGRESS_ENV_NAMES",
    "EGRESS_REFUSED_SOURCE",
    "EgressProxy",
    "Resolver",
    "destination_allowed",
    "host_of",
]

EGRESS_REFUSED_SOURCE: Final[str] = "runner.egress_refused"

# Both spellings: curl and git read the lower-case ones, most language runtimes the
# upper. Reserved on a Directive's environment so a hook cannot point the Agent round it.
EGRESS_ENV_NAMES: Final[frozenset[str]] = frozenset(
    {
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "https_proxy",
        "http_proxy",
        "all_proxy",
        "no_proxy",
    }
)

# Loopback is the LLM proxy, the callback socket's neighbours and every Runner-hosted MCP
# server: all the Runner's own, none of it egress.
_LOOPBACK: Final[str] = "127.0.0.1,localhost,::1"
_PROXY_USER: Final[str] = "agentic"
_PIPE_CHUNK: Final[int] = 65536

Resolver = Callable[[str, int], Awaitable[tuple[str, int]]]


async def _system_resolver(host: str, port: int) -> tuple[str, int]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=0)
    address = infos[0][4]
    return str(address[0]), int(address[1])


def destination_allowed(allow_list: Iterable[str], host: str, port: int) -> bool:
    """``host``, ``host:port`` or a ``*.domain`` pattern; a bare host admits any port."""

    host = host.lower().strip("[]")
    for entry in allow_list:
        pattern, _, entry_port = entry.lower().partition(":")
        if entry_port and entry_port != str(port):
            continue
        if fnmatchcase(host, pattern):
            return True
    return False


def host_of(url: str) -> str | None:
    """The allow-list entry a URL needs: its host, with the port when it names one."""

    parts = urlsplit(url)
    if not parts.hostname:
        return None
    return parts.hostname if parts.port is None else f"{parts.hostname}:{parts.port}"


class EgressProxy:
    """One attempt's forward proxy: CONNECT for TLS, absolute-form for plain HTTP."""

    def __init__(
        self,
        allow_list: Iterable[str],
        *,
        token: str,
        resolver: Resolver | None = None,
        on_refused: Callable[[str, int], Awaitable[None]] | None = None,
        host: str = "127.0.0.1",
    ) -> None:
        self.allow_list = tuple(dict.fromkeys(allow_list))
        self._token = token
        self._resolver = resolver or _system_resolver
        self._on_refused = on_refused
        self._host = host
        self._server: asyncio.Server | None = None
        self._port = 0

    async def __aenter__(self) -> EgressProxy:
        self._server = await asyncio.start_server(self._serve, host=self._host, port=0)
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

    def env(self) -> dict[str, str]:
        url = f"http://{_PROXY_USER}:{self._token}@{self._host}:{self._port}"
        env = {name: url for name in EGRESS_ENV_NAMES if "no_proxy" not in name.lower()}
        env.update({"NO_PROXY": _LOOPBACK, "no_proxy": _LOOPBACK})
        return env

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request = await read_request(reader)
            if isinstance(request, tuple):
                await self._reply(writer, request[0])
                return
            if not self._authorized(request):
                await self._reply(writer, 407)
                return
            destination = _destination(request)
            if destination is None:
                await self._reply(writer, 400)
                return
            host, port = destination
            if not destination_allowed(self.allow_list, host, port):
                if self._on_refused is not None:
                    await self._on_refused(host, port)
                await self._reply(writer, 403)
                return
            try:
                address, resolved_port = await self._resolver(host, port)
                upstream_reader, upstream_writer = await asyncio.open_connection(
                    address, resolved_port
                )
            except OSError:
                await self._reply(writer, 502)
                return
            if request.method == "CONNECT":
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()
            else:
                upstream_writer.write(_origin_form(request))
                await upstream_writer.drain()
            await _pipe_both(reader, writer, upstream_reader, upstream_writer)
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    def _authorized(self, request: HttpRequest) -> bool:
        scheme, _, encoded = request.headers.get("proxy-authorization", "").partition(" ")
        if scheme.lower() != "basic":
            return False
        try:
            _, _, offered = base64.b64decode(encoded.strip()).decode().partition(":")
        except (ValueError, UnicodeDecodeError):
            return False
        return hmac.compare_digest(offered, self._token)

    @staticmethod
    async def _reply(writer: asyncio.StreamWriter, status: int) -> None:
        head = response_head(status, content_type="text/plain", content_length=0)
        if status == 407:
            head = head.replace(
                b"\r\n\r\n", b'\r\nProxy-Authenticate: Basic realm="agentic-runner"\r\n\r\n', 1
            )
        writer.write(head)
        with contextlib.suppress(ConnectionError):
            await writer.drain()


def _destination(request: HttpRequest) -> tuple[str, int] | None:
    if request.method == "CONNECT":
        host, _, port = request.target.rpartition(":")
        return (host, int(port)) if host and port.isdigit() else None
    parts = urlsplit(request.target)
    if parts.scheme != "http" or not parts.hostname:
        return None
    return parts.hostname, parts.port or 80


def _origin_form(request: HttpRequest) -> bytes:
    parts = urlsplit(request.target)
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    headers = {
        name: value
        for name, value in request.headers.items()
        if name not in {"proxy-authorization", "proxy-connection", "connection"}
    }
    head = f"{request.method} {path} HTTP/1.1\r\n" + "".join(
        f"{name}: {value}\r\n" for name, value in headers.items()
    )
    return (head + "Connection: close\r\n\r\n").encode("latin-1") + request.body


async def _pipe_both(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    upstream_reader: asyncio.StreamReader,
    upstream_writer: asyncio.StreamWriter,
) -> None:
    async def pipe(source: asyncio.StreamReader, sink: asyncio.StreamWriter) -> None:
        with contextlib.suppress(ConnectionError, asyncio.IncompleteReadError):
            while chunk := await source.read(_PIPE_CHUNK):
                sink.write(chunk)
                await sink.drain()
        with contextlib.suppress(Exception):
            sink.write_eof()

    try:
        await asyncio.gather(
            pipe(client_reader, upstream_writer), pipe(upstream_reader, client_writer)
        )
    finally:
        upstream_writer.close()
        with contextlib.suppress(Exception):
            await upstream_writer.wait_closed()
