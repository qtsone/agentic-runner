"""The per-attempt callback socket (ADR-0013 §10, map ticket 26 §4, 25 §10).

Buildkite's Job API, narrowed to what ADR-0011 §9 can defend. For each Directive attempt
the Runner mints 32 random bytes as a bearer and opens a Unix socket **owned by the
Contract's uid** (map ticket 17 A11); the Agent Runtime subprocess receives the socket
path and the bearer in its environment and nothing else. ``agentic-runner annotate |
artifact upload | verb | repo`` speak to it.

What makes that not a credential: the token's only audience is this Runner's own socket,
it is useless off the box, it dies with the attempt, and it is a *correlator* rather than
an authority — a ``verb`` call is one more entry point into the same Grant evaluation the
activity seams run, attributed to the Agent of that attempt, never a bypass (ADR-0011
§10). The same bearer is what the relocated LLM proxy accepts (PRD issue 43), so one
token per attempt serves both.

**Not offered**, deliberately: a `pipeline upload` analogue (an Agent rewriting its own
loop is the thing the platform owns), `meta-data`, and any route that returns a
credential — :data:`ROUTES` is the closed set and
``tests/unit/test_runner_callback_socket.py`` asserts no field in it is credential-shaped.
"""

from __future__ import annotations

import asyncio
import os
import secrets
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, Final, Literal, Self

import httpx
from pydantic import BaseModel, Field

from agentic_runner.tiny_http import (
    MAX_HEADER_BYTES,
    HttpRequest,
    bearer_matches,
    read_request,
    write_json,
)
from agentic_runner_contracts.channel_messages import (
    MESSAGE_BODY_MAX_BYTES,
    MessageEnvelope,
    MessageKind,
    MessageReference,
)
from agentic_runner_contracts.questions import QUESTION_TEXT_MAX

__all__ = [
    "CALLBACK_SOCKET_ENV",
    "CALLBACK_TOKEN_ENV",
    "ROUTES",
    "AnnotateRequest",
    "AnnotateResponse",
    "AskRequest",
    "AskResponse",
    "ArtifactRequest",
    "ArtifactResponse",
    "AttemptCallbackServer",
    "CallbackError",
    "CallbackHandlers",
    "MessageListRequest",
    "MessageListResponse",
    "MessageSendRequest",
    "MessageSendResponse",
    "RepoRequest",
    "RepoResponse",
    "Route",
    "VerbRequest",
    "VerbResponse",
    "call",
]

CALLBACK_SOCKET_ENV: Final[str] = "AGENTIC_RUNNER_CALLBACK_SOCKET"
CALLBACK_TOKEN_ENV: Final[str] = "AGENTIC_RUNNER_CALLBACK_TOKEN"

_TOKEN_BYTES: Final[int] = 32
# `sockaddr_un.sun_path` is 108 bytes on Linux and 104 on macOS, and the failure mode of
# overrunning it is a truncated path that binds somewhere else entirely. The Workspace
# root plus a Contract id plus a Work Record id is already past it, which is why the
# socket lives in its own short directory rather than beside the checkout.
MAX_SOCKET_PATH_BYTES: Final[int] = 100


class AnnotateRequest(BaseModel):
    context: str = Field(max_length=100)
    body: str = Field(max_length=65_536)
    style: str = Field(default="info", pattern=r"^(info|success|warning|error)$")


class AnnotateResponse(BaseModel):
    accepted: bool
    context: str


class ArtifactRequest(BaseModel):
    """One file already in the Workspace, offered for upload by path.

    Never file *contents*: the Runner reads the path itself as the Contract's data, which
    keeps a multi-gigabyte upload off the socket and off the Runner's heap.
    """

    path: str = Field(max_length=4096)
    label: str = Field(default="", max_length=200)


class ArtifactResponse(BaseModel):
    accepted: bool
    path: str
    size_bytes: int
    sha256: str
    reason: str = ""


class VerbRequest(BaseModel):
    verb: str = Field(max_length=100)
    resource: str = Field(max_length=400)


class VerbResponse(BaseModel):
    """The seam's own verdict. No token, no credential — only what was decided and why."""

    verb: str
    resource: str
    decision: str
    allowed: bool
    reason: str


class RepoRequest(BaseModel):
    """``agentic-runner repo read | branch`` (ADR-0018 §5, §7): one repository, by name.

    ``base_ref`` is for `branch` only; empty cuts the work branch from the repository's
    default branch.
    """

    repository: str = Field(max_length=200, pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    base_ref: str = Field(default="", max_length=255)


class RepoResponse(BaseModel):
    """The seam's verdict and, when allowed, where the checkout is in the Workspace."""

    repository: str
    allowed: bool
    decision: str
    reason: str
    path: str = ""
    product_id: str = ""


class MessageSendRequest(BaseModel):
    """``agentic-runner message send`` (PRD issue 52): the envelope minus what the Runner
    fills in itself -- sender, Work Record, id and time are the attempt's, never the
    caller's to claim."""

    channel_id: str = Field(max_length=64)
    kind: MessageKind = MessageKind.NOTE
    body: str = Field(max_length=MESSAGE_BODY_MAX_BYTES)
    references: list[MessageReference] = Field(default_factory=list, max_length=32)
    # PRD issue 53: who this is for. A `result` with neither is addressed to the sender
    # of the `message` it references, which the Runner resolves from its own store.
    recipient_agent_id: str | None = Field(default=None, max_length=64)
    recipient_role: str | None = Field(default=None, max_length=32)
    # PRD issue 54: a `verdict` names the head commit it reviewed and carries one bit.
    verdict: Literal["block", "clear"] | None = None
    head_sha: str | None = Field(default=None, max_length=64)


class MessageSendResponse(BaseModel):
    """Whether the `channel.write` seam let it through, and the id it got if so."""

    accepted: bool
    decision: str
    reason: str
    message_id: str = ""


class MessageListRequest(BaseModel):
    channel_id: str = Field(max_length=64)


class MessageListResponse(BaseModel):
    """The Channel's Messages under `channel.read`; empty with the decision otherwise."""

    allowed: bool
    decision: str
    reason: str
    messages: list[MessageEnvelope] = Field(default_factory=list)


class AskRequest(BaseModel):
    """``agentic-runner ask`` (PRD issue 60): the Question's text and nothing else --
    the asking Agent, the Work Record and the addressee are the attempt's to know."""

    text: str = Field(min_length=1, max_length=QUESTION_TEXT_MAX)


class AskResponse(BaseModel):
    """`work.ask`'s verdict. ``accepted`` means the Question stands: the Work Record holds
    on it once this Directive ends, and the next Directive carries the answer (or the
    fact that none came). A refusal leaves the Directive to carry on without it."""

    accepted: bool
    decision: str
    reason: str
    question_id: str = ""
    addressed_to: str = ""


@dataclass(frozen=True, slots=True)
class Route:
    request: type[BaseModel]
    response: type[BaseModel]
    handler: str


ROUTES: Final[dict[tuple[str, str], Route]] = {
    ("POST", "/v0/annotate"): Route(AnnotateRequest, AnnotateResponse, "annotate"),
    ("POST", "/v0/artifact"): Route(ArtifactRequest, ArtifactResponse, "artifact"),
    ("POST", "/v0/verb"): Route(VerbRequest, VerbResponse, "verb"),
    ("POST", "/v0/message/send"): Route(MessageSendRequest, MessageSendResponse, "message_send"),
    ("POST", "/v0/message/list"): Route(MessageListRequest, MessageListResponse, "message_list"),
    ("POST", "/v0/ask"): Route(AskRequest, AskResponse, "ask"),
    ("POST", "/v0/repo/read"): Route(RepoRequest, RepoResponse, "repo_read"),
    ("POST", "/v0/repo/branch"): Route(RepoRequest, RepoResponse, "repo_branch"),
}


@dataclass(frozen=True, slots=True)
class CallbackHandlers:
    """What the Runner does when a Directive calls back. Injected by the activity."""

    annotate: Callable[[AnnotateRequest], Awaitable[AnnotateResponse]]
    artifact: Callable[[ArtifactRequest], Awaitable[ArtifactResponse]]
    verb: Callable[[VerbRequest], Awaitable[VerbResponse]]
    message_send: Callable[[MessageSendRequest], Awaitable[MessageSendResponse]]
    message_list: Callable[[MessageListRequest], Awaitable[MessageListResponse]]
    ask: Callable[[AskRequest], Awaitable[AskResponse]]
    repo_read: Callable[[RepoRequest], Awaitable[RepoResponse]]
    repo_branch: Callable[[RepoRequest], Awaitable[RepoResponse]]


class CallbackError(RuntimeError):
    """A callback the Runner refused: the status and the body it answered with."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(f"callback refused ({status_code}): {detail}")
        self.status_code = status_code
        self.detail = detail


class AttemptCallbackServer:
    """One attempt's socket. Opened on enter, unlinked on exit; the token dies with it."""

    def __init__(
        self,
        *,
        socket_path: Path,
        handlers: CallbackHandlers,
        uid: int | None = None,
        token: str | None = None,
    ) -> None:
        if len(os.fsencode(socket_path)) > MAX_SOCKET_PATH_BYTES:
            raise ValueError(
                f"callback socket path is longer than {MAX_SOCKET_PATH_BYTES} bytes: {socket_path}"
            )
        self._socket_path = socket_path
        self._handlers = handlers
        self._uid = uid
        self._token = token or secrets.token_urlsafe(_TOKEN_BYTES)
        self._server: asyncio.AbstractServer | None = None

    @property
    def token(self) -> str:
        return self._token

    @property
    def socket_path(self) -> Path:
        return self._socket_path

    def env(self) -> dict[str, str]:
        """The two names the Agent Runtime subprocess gains, and nothing else."""

        return {CALLBACK_SOCKET_ENV: str(self._socket_path), CALLBACK_TOKEN_ENV: self._token}

    async def __aenter__(self) -> Self:
        self._socket_path.unlink(missing_ok=True)
        # `limit` is what actually bounds the request head: `readuntil` raises
        # LimitOverrunError at the reader's own limit, so leaving it at the 64 KiB default
        # would mean the 431 below never fires and an oversized head answered 500.
        self._server = await asyncio.start_unix_server(
            self._serve, path=str(self._socket_path), limit=MAX_HEADER_BYTES
        )
        # Order matters: 0600 before the chown, so there is no window in which the socket
        # is both world-writable and owned by the Contract.
        os.chmod(self._socket_path, 0o600)
        if self._uid is not None:
            os.chown(self._socket_path, self._uid, self._uid)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.close()
            await server.wait_closed()
        self._socket_path.unlink(missing_ok=True)

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            status, payload = await self._handle(reader)
        except Exception as error:  # noqa: BLE001 - a handler fault answers 500, never kills the socket
            status, payload = 500, {"detail": error.__class__.__name__}
        await write_json(writer, status, payload)
        writer.close()

    async def _handle(self, reader: asyncio.StreamReader) -> tuple[int, dict[str, Any]]:
        request = await read_request(reader)
        if not isinstance(request, HttpRequest):
            return request
        if not bearer_matches(request.authorization, self._token):
            return 401, {"detail": "a valid attempt bearer is required"}
        route = ROUTES.get((request.method, request.target))
        if route is None:
            return 404, {"detail": "no such callback route"}
        try:
            parsed = route.request.model_validate_json(request.body or b"{}")
        except ValueError as error:
            return 422, {"detail": str(error)[:500]}
        handler: Callable[[BaseModel], Awaitable[BaseModel]] = getattr(
            self._handlers, route.handler
        )
        return 200, (await handler(parsed)).model_dump(mode="json")


def call(
    *,
    socket_path: str | Path,
    token: str,
    path: str,
    payload: Mapping[str, Any],
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    """One callback, over the attempt's own socket. Used by the ``agentic-runner`` CLI."""

    transport = httpx.HTTPTransport(uds=str(socket_path))
    with httpx.Client(transport=transport, timeout=timeout_seconds) as client:
        response = client.post(
            f"http://localhost{path}",
            json=dict(payload),
            headers={"Authorization": f"Bearer {token}"},
        )
    if response.status_code != 200:
        raise CallbackError(response.status_code, _detail(response))
    decoded: dict[str, Any] = response.json()
    return decoded


def _detail(response: httpx.Response) -> str:
    try:
        return str(response.json().get("detail", response.text))
    except ValueError:
        return response.text
