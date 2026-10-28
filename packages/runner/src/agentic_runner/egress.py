"""Scoped egress for one Directive (PRD issue 58, map 06 §4, ticket 05's open item).

``CODEX_NETWORK_ACCESS`` was all-or-nothing: a Directive either reached the whole internet
or nothing, and a VPC-only Streamable HTTP MCP server needs neither. The network part of
the Directive's Guardrails now names the destinations it may reach; the Runner
adds the ones it knows the Directive needs (the git remote, each granted HTTP MCP
server), and this forward proxy on loopback is where the list is enforced: every
Directive under a Guardrails allow-list is handed ``HTTPS_PROXY`` / ``HTTP_PROXY`` /
``ALL_PROXY`` naming it, with the attempt's bearer as the proxy credential so another
Contract's uid on the same loopback cannot borrow this attempt's list.

What this is *not* is a kernel boundary. A process that ignores the proxy variables and
opens a raw socket is stopped only by what the host adds underneath (a pod
NetworkPolicy); a workstation adds nothing, which is why it reports its posture as
``unenforced`` in the heartbeat rather than claiming the list binds.

The destinations a narrowing layer removed are a deny-list in the same syntax, read by the
same proxy after the allow-list: a destination passes only if the Organisation allows it
and no layer removed it. Both lists are compared through :func:`parse_destination`, so a
case, a trailing dot, a port or an IP literal's spelling means the same in either.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hmac
import ipaddress
import re
import socket
from collections.abc import Awaitable, Callable, Iterable, Sequence
from fnmatch import fnmatchcase
from types import TracebackType
from typing import Final
from urllib.parse import urlsplit

from agentic_runner.tiny_http import HttpRequest, read_request, response_head

__all__ = [
    "EGRESS_ENV_NAMES",
    "EgressProxy",
    "Resolver",
    "destination_allowed",
    "destination_denied",
    "host_of",
    "parse_destination",
    "url_destination",
]


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
_INET_ATON_CHARS: Final[re.Pattern[str]] = re.compile(r"[0-9a-fx.]+")

Resolver = Callable[[str, int], Awaitable[tuple[str, int]]]


async def _system_resolver(host: str, port: int) -> tuple[str, int]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=0)
    address = infos[0][4]
    return str(address[0]), int(address[1])


def normalise_host(host: str) -> str:
    """A host as both lists compare it: lower case, no brackets, no trailing dot, and an IP
    literal in its one canonical spelling (an IPv4-mapped IPv6 address as the IPv4 one)."""

    host = host.strip().lower().strip("[]").rstrip(".")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return _inet_aton_spelling(host) or host
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return str(address.ipv4_mapped)
    return address.compressed


def _inet_aton_spelling(host: str) -> str | None:
    """The dotted quad for an IPv4 address in a spelling ``ipaddress`` refuses and
    ``getaddrinfo`` still connects to (``167772165``, ``0xa.0.0.5``, ``10.5``, ``012.0.0.5``):
    left as a name, it would walk past a deny entry for the address it reaches."""

    # Only the digits, hex and dots inet_aton parses: glibc's also accepts trailing garbage.
    if not _INET_ATON_CHARS.fullmatch(host):
        return None
    try:
        return socket.inet_ntoa(socket.inet_aton(host))
    except OSError:
        return None


def parse_destination(entry: str) -> tuple[str, int | None]:
    """A Guardrails network destination as ``(host pattern, port)``; None admits any port.

    ``host``, ``host:port``, ``*.domain``, ``[v6]:port`` or a bare IPv6 literal. Raises
    ``ValueError`` for an entry whose port is not one.
    """

    text = entry.strip()
    if text.startswith("["):
        host, closed, rest = text[1:].partition("]")
        if not closed or (rest and not rest.startswith(":")):
            raise ValueError(f"{entry!r} is not a destination")
        raw_port: str | None = rest[1:] if rest else None
    elif text.count(":") == 1:
        host, _, raw_port = text.partition(":")
    else:
        # A bare host, or an IPv6 literal written without brackets and so without a port.
        host, raw_port = text, None
    if not host.strip():
        raise ValueError(f"{entry!r} names no host")
    if raw_port is None:
        return normalise_host(host), None
    if not raw_port.isdigit() or not 0 < int(raw_port) < 65536:
        raise ValueError(f"{entry!r} has a port that is not a number from 1 to 65535")
    return normalise_host(host), int(raw_port)


def _matches(entry: str, host: str, port: int) -> bool:
    try:
        pattern, entry_port = parse_destination(entry)
    except ValueError:
        # Never a match, as before parsing: an allow-list entry that is not a destination
        # admits nothing, and the Runner Limit refuses to start on one (`runner_limit`).
        return False
    if entry_port is not None and entry_port != port:
        return False
    return fnmatchcase(host, pattern)


def destination_allowed(allow_list: Iterable[str], host: str, port: int) -> bool:
    """``host``, ``host:port`` or a ``*.domain`` pattern; a bare host admits any port."""

    host = normalise_host(host)
    return any(_matches(entry, host, port) for entry in allow_list)


def destination_denied(deny_list: Sequence[str], host: str, port: int) -> int | None:
    """The position of the first Runner Limit ``networks`` entry denying this, or None.

    A hostname entry denies that name only: the proxy compares what the Directive asked
    for, never what it resolves to (ADR-0022 §54).
    """

    host = normalise_host(host)
    return next(
        (index for index, entry in enumerate(deny_list) if _matches(entry, host, port)), None
    )


def host_of(url: str) -> str | None:
    """The allow-list entry a URL needs: its host, with the port when it names one."""

    parts = urlsplit(url)
    if not parts.hostname:
        return None
    return parts.hostname if parts.port is None else f"{parts.hostname}:{parts.port}"


def url_destination(url: str) -> tuple[str, int] | None:
    """Where a URL connects: its host and its port, the scheme's default when it names none."""

    parts = urlsplit(url)
    if not parts.hostname:
        return None
    try:
        port = parts.port
    except ValueError:
        return None
    return parts.hostname, port or (80 if parts.scheme in {"http", "ws"} else 443)


class EgressProxy:
    """One attempt's forward proxy: CONNECT for TLS, absolute-form for plain HTTP."""

    def __init__(
        self,
        allow_list: Iterable[str] | None,
        *,
        token: str,
        deny_list: Sequence[str] = (),
        resolver: Resolver | None = None,
        on_refused: Callable[[str, int, int | None], Awaitable[None]] | None = None,
        host: str = "127.0.0.1",
    ) -> None:
        # None: no Profile allow-list, so only the Runner Limit's `networks` can refuse.
        self.allow_list = None if allow_list is None else tuple(dict.fromkeys(allow_list))
        self.deny_list = tuple(deny_list)
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
            # The Profile's list first, as the Grant is named before the Runner Limit: a
            # destination both refuse is refused for the reason no other Runner would lift.
            allowed = self.allow_list is None or destination_allowed(self.allow_list, host, port)
            denied_by = None if not allowed else destination_denied(self.deny_list, host, port)
            if not allowed or denied_by is not None:
                if self._on_refused is not None:
                    await self._on_refused(host, port, denied_by)
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
