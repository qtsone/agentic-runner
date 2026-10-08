"""The few hundred bytes of HTTP the Runner serves to its own Directives.

Two servers live behind it: the per-attempt callback socket (PRD issue 45) and the
relocated LLM proxy (issue 43). Neither is a web server — one speaks over a Unix socket
the Contract's uid owns, the other over loopback TCP — and both answer a closed set of
routes to a caller that is a subprocess this same process spawned.

So: ``asyncio.start_unix_server``/``start_server`` and this module, rather than a web
framework in a package that gets shipped to clients. The proxy also has to *stream* an
upstream SSE body straight through, which is why responses are written as head-then-body
rather than returned whole.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
from dataclasses import dataclass
from typing import Any, Final

__all__ = [
    "MAX_BODY_BYTES",
    "MAX_HEADER_BYTES",
    "HttpRequest",
    "bearer_matches",
    "read_request",
    "response_head",
    "write_json",
]

MAX_HEADER_BYTES: Final[int] = 8192
MAX_BODY_BYTES: Final[int] = 1024 * 1024

_REASONS: Final[dict[int, bytes]] = {
    200: b"OK",
    202: b"Accepted",
    400: b"Bad Request",
    401: b"Unauthorized",
    402: b"Payment Required",
    403: b"Forbidden",
    404: b"Not Found",
    405: b"Method Not Allowed",
    407: b"Proxy Authentication Required",
    413: b"Payload Too Large",
    422: b"Unprocessable Entity",
    431: b"Request Header Fields Too Large",
    500: b"Internal Server Error",
    502: b"Bad Gateway",
    504: b"Gateway Timeout",
    503: b"Service Unavailable",
}


@dataclass(frozen=True, slots=True)
class HttpRequest:
    method: str
    target: str
    headers: dict[str, str]
    body: bytes

    @property
    def authorization(self) -> str:
        return self.headers.get("authorization", "")


async def read_request(
    reader: asyncio.StreamReader,
    *,
    max_header_bytes: int = MAX_HEADER_BYTES,
    max_body_bytes: int = MAX_BODY_BYTES,
) -> HttpRequest | tuple[int, dict[str, Any]]:
    """One request, or the ``(status, body)`` to answer instead of parsing it."""

    try:
        head = await reader.readuntil(b"\r\n\r\n")
    except asyncio.LimitOverrunError:
        return 431, {"detail": "request headers too large"}
    except (asyncio.IncompleteReadError, ConnectionError):
        return 400, {"detail": "the request head was incomplete"}
    if len(head) > max_header_bytes:
        return 431, {"detail": "request headers too large"}
    lines = head.decode("latin-1").split("\r\n")
    method, _, rest = lines[0].partition(" ")
    target = rest.rpartition(" ")[0].strip() or rest.strip()
    headers = {
        name.strip().lower(): value.strip()
        for name, _, value in (line.partition(":") for line in lines[1:])
        if name
    }
    try:
        length = int(headers.get("content-length", "0") or 0)
    except ValueError:
        return 400, {"detail": "content-length is not a number"}
    if length > max_body_bytes:
        return 413, {"detail": "request body too large"}
    body = await reader.readexactly(length) if length > 0 else b""
    return HttpRequest(method=method.upper(), target=target, headers=headers, body=body)


def response_head(
    status: int,
    *,
    content_type: str,
    content_length: int | None = None,
) -> bytes:
    """The status line and headers. ``content_length=None`` leaves the body open-ended,
    closed by the connection itself — which is how an SSE relay ends."""

    head = (
        f"HTTP/1.1 {status} {_REASONS.get(status, b'Unknown').decode()}\r\n"
        f"Content-Type: {content_type}\r\n"
    )
    if content_length is not None:
        head += f"Content-Length: {content_length}\r\n"
    return (head + "Connection: close\r\n\r\n").encode("latin-1")


async def write_json(writer: asyncio.StreamWriter, status: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload).encode()
    writer.write(response_head(status, content_type="application/json", content_length=len(body)))
    writer.write(body)
    with contextlib.suppress(ConnectionResetError, BrokenPipeError):
        await writer.drain()


def bearer_matches(header: str, token: str) -> bool:
    """Constant time: a bearer is the only thing standing between one Contract's uid and
    another attempt's socket, and a socket accepts as many guesses as it is given."""

    scheme, _, offered = header.partition(" ")
    return scheme.lower() == "bearer" and hmac.compare_digest(offered.strip(), token)
