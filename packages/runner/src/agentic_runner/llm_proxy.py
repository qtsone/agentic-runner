"""The LLM proxy, on the Runner (PRD issue 43, ADR-0013 §4 and §11, ADR-0015 §4).

The funder's key never crosses the boundary (map decisions 5 and 8), so the metering
point moves to the host that holds it. Issue 13 built the Usage Record and the ceiling
semantics against the backend proxy; this is the same semantics, on the Runner:

* **One proxy per Runner process, on loopback TCP.** The CLIs need a URL, not a socket
  (17 A11). Each Directive attempt is registered under its own path segment and is
  admitted only by the per-attempt bearer issue 45 already mints for the callback socket
  — one token per attempt serves both — so another attempt's bearer is a 401 before any
  provider is called. The Agent Runtime subprocess gets that URL and that bearer and
  never a provider key (ADR-0011 §9, unchanged).
* **The slot is resolved per request, never per Directive** (22 A8). A value the funder
  replaced takes effect on every running Directive at its next call, and no Directive is
  stranded on a key revoked at the provider.
* **A new value is probe-gated** (22 A10): it enters service only after a zero-cost
  models-list probe answers `valid`; an invalid one is refused, the old one keeps
  serving, and the failure shows on the heartbeat's slot fields.
* **Ceilings are enforced per call** (map ticket 12 B4), against values the control plane
  pushes. Over either one, the call is refused with issue 13's typed error and nothing is
  spent. **No model degradation** — a silent swap to a cheaper model would make the
  ledger lie (12 B5).
* **Usage goes out over the heartbeat stream** (ADR-0013 §11), batched, acknowledged and
  retried, idempotent on ``(directive_id, sequence)``.

Counts and ids leave this module; prompts and completions do not. The only part of a
provider response read here is its ``usage`` block, which is what keeps ADR-0010 §4 true
now that the proxy is no longer control-plane code.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, Final, Self
from uuid import UUID

import httpx

from agentic_runner.tiny_http import (
    HttpRequest,
    bearer_matches,
    read_request,
    response_head,
    write_json,
)
from agentic_runner_contracts.llm_usage import (
    CeilingExhaustedError,
    ModelPrice,
    UsageRecord,
    ceiling_exhausted_detail,
    resolve_price,
)
from agentic_runner_contracts.runner_registration import (
    USAGE_BATCH_MAX,
    SlotProbe,
    SlotStatus,
)

__all__ = [
    "ATTEMPT_PREFIX",
    "COMPLETION_PATHS",
    "CALL_REFUSED_SOURCE",
    "PROXY_API_KEY_ENV",
    "PROXY_BASE_URL_ENV",
    "PROXY_ENV_NAMES",
    "AttemptHandle",
    "CeilingStore",
    "Ceilings",
    "CredentialSlot",
    "LLM_SLOT_REFERENCES",
    "LlmProvider",
    "LlmProxy",
    "SLOT_REFUSED_SOURCE",
    "SLOT_SWAPPED_SOURCE",
    "SlotStore",
    "UsageOutbox",
    "attempt_env",
    "llm_slot_references",
]

ATTEMPT_PREFIX: Final[str] = "/a/"

# The OpenAI-compatible half of `attempt_env`. Codex does not read the base URL from the
# environment, so its runtime lifts it from here into a provider of its own.
PROXY_BASE_URL_ENV: Final[str] = "OPENAI_BASE_URL"
PROXY_API_KEY_ENV: Final[str] = "OPENAI_API_KEY"

# Every name `attempt_env` may set. Reserved on a Directive's environment so no hook
# can redirect an Agent's traffic away from the metering point (`_runtime_support`).
PROXY_ENV_NAMES: Final[frozenset[str]] = frozenset(
    {"ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", PROXY_BASE_URL_ENV, PROXY_API_KEY_ENV}
)

# A Directive's context is the whole reason these bodies are large: Claude Code and Codex
# both send the assembled prompt on every turn, and 1 MiB (the callback socket's bound)
# is under what one long Work Record actually sends.
# ponytail: a flat 8 MiB read into memory. Streaming the request body upstream would
# avoid the copy — worth doing if a Runner's RSS ever shows it.
MAX_REQUEST_BODY_BYTES: Final[int] = 8 * 1024 * 1024

# The Evidence sources a refusal and a swap append under (issue 43: ids and a reason,
# never a value and never a body).
CALL_REFUSED_SOURCE: Final[str] = "llm_call_refused"
SLOT_SWAPPED_SOURCE: Final[str] = "llm_slot_swapped"
SLOT_REFUSED_SOURCE: Final[str] = "llm_slot_refused"

_TOKEN_BYTES: Final[int] = 32
_LOOPBACK_HOSTS: Final[frozenset[str]] = frozenset({"127.0.0.1", "::1", "localhost"})

# The completion routes, relayed to the same path at the slot's own base URL: the
# OpenAI-compatible one the backend proxy served, Anthropic's, because Claude Code is one
# of the two harnesses the platform ships and it would otherwise still need a provider key
# of its own (ADR-0011 §9), and the Responses API, which is the only wire Codex speaks.
COMPLETION_PATHS: Final[frozenset[str]] = frozenset(
    {"/chat/completions", "/messages", "/responses"}
)
_SSE_DONE: Final[bytes] = b"data: [DONE]"

# Anthropic rejects a request without it, whichever credential shape the request carries.
ANTHROPIC_VERSION: Final[str] = "2023-06-01"

# What the proxy does not pass upstream: hop-by-hop headers, the attempt's own bearer
# (the slot's credential takes its place) and what httpx recomputes for the body it is
# handed. Everything else is the harness's own and is forwarded -- `anthropic-beta`
# selects the features Claude Code depends on, and dropping it silently changes the wire
# the Agent thinks it is speaking.
_HEADERS_NOT_FORWARDED: Final[frozenset[str]] = frozenset(
    {
        "accept-encoding",
        "authorization",
        "connection",
        "content-length",
        "host",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "x-api-key",
    }
)

Evidence = Callable[[str, Mapping[str, object]], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class CredentialSlot:
    """One Contract's LLM credential, as the Runner holds it (ADR-0013 §11).

    ``value`` is plaintext in this process's memory and nowhere else: it is never
    logged, never written to disk and never put on the heartbeat — what the funder sees
    is ``key_id`` and the probe result (22 A10).

    ``auth_style`` is two shapes because the platform ships two harnesses: a bearer
    (OpenAI-compatible endpoints, and a ``claude setup-token`` bearer) and Anthropic's
    ``x-api-key``. Not a plugin point — a third provider adds a branch.
    """

    reference: str
    key_id: str
    provider_name: str
    base_url: str
    value: str
    runtime_kind: str = "codex"
    auth_style: str = "bearer"
    delivered_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def headers(self) -> dict[str, str]:
        """The credential, plus the version header Anthropic refuses a request without.

        ``anthropic-version`` goes on both auth styles, not just ``x-api-key``: a
        ``claude setup-token`` bearer is the same wire, and without the header the
        models-list probe answers 400 and the slot never enters service at all (22 A10).
        """

        headers = (
            {"x-api-key": self.value}
            if self.auth_style == "x-api-key"
            else {"Authorization": f"Bearer {self.value}"}
        )
        if self.auth_style == "x-api-key" or self.runtime_kind.startswith("claude"):
            headers["anthropic-version"] = ANTHROPIC_VERSION
        return headers

    def url(self, path: str) -> str:
        return f"{self.base_url.rstrip('/')}/{path.lstrip('/')}"


@dataclass(frozen=True, slots=True)
class LlmProvider:
    """Where a delivered key is spent, and how it is presented there.

    Endpoints are plain config, never a secret (ADR-0013 §11), so the sealed wire does not
    carry one: the Credential Reference's name says which provider a value belongs to and
    this says where that provider is.
    """

    name: str
    runtime_kind: str
    base_url: str
    auth_style: str

    def slot(self, *, reference: str, key_id: str, value: str) -> CredentialSlot:
        return CredentialSlot(
            reference=reference,
            key_id=key_id,
            provider_name=self.name,
            base_url=self.base_url,
            value=value,
            runtime_kind=self.runtime_kind,
            auth_style=self.auth_style,
        )


# The Credential References a delivered value fills the proxy's slot from (local-agents
# 04b), named after the variable each harness reads its own key from, so a funder declares
# the name the vendor's docs already taught them. Any other reference stays a verb-seam or
# MCP credential and never reaches the proxy.
LLM_SLOT_REFERENCES: Final[Mapping[str, LlmProvider]] = {
    "OPENAI_API_KEY": LlmProvider(
        name="openai",
        runtime_kind="codex_cli",
        base_url="https://api.openai.com/v1",
        auth_style="bearer",
    ),
    "ANTHROPIC_API_KEY": LlmProvider(
        name="anthropic",
        runtime_kind="claude_code",
        base_url="https://api.anthropic.com/v1",
        auth_style="x-api-key",
    ),
}


def llm_slot_references(base_urls: Mapping[str, str]) -> dict[str, LlmProvider]:
    """:data:`LLM_SLOT_REFERENCES` with an operator's endpoint per provider name -- a
    gateway in front of the vendor, or the chart test's fake provider."""

    return {
        reference: replace(provider, base_url=base_urls.get(provider.name, provider.base_url))
        for reference, provider in LLM_SLOT_REFERENCES.items()
    }


SlotProber = Callable[[CredentialSlot], Awaitable[bool]]


async def models_list_probe(slot: CredentialSlot) -> bool:
    """The zero-cost probe (22 A10): can this value list models at its provider?

    A models list bills nothing and answers the only question that matters before a value
    goes into service — whether the provider still accepts it. A network fault answers
    *no*, and the caller keeps the value already serving: putting an unverified value in
    on a timeout is exactly the swap this gate exists to stop.
    """

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(slot.url("models"), headers=slot.headers())
    except httpx.HTTPError:
        return False
    return response.status_code == 200


class SlotStore:
    """Every Contract's current LLM slot, resolved per request (22 A8).

    Mutated by delivery, read by every call. The read is a plain dictionary lookup on
    purpose: it happens inside the request, so a replacement lands on every running
    Directive at its next call rather than at its next Directive.
    """

    def __init__(self, *, probe: SlotProber = models_list_probe) -> None:
        self._probe = probe
        self._slots: dict[UUID, CredentialSlot] = {}
        self._probes: dict[UUID, SlotProbe] = {}
        self._last_used: dict[UUID, datetime] = {}
        # Leaf Contract -> the Contract whose slot funds it (22 A6). Data from issue 34;
        # the proxy follows the mapping and never derives it.
        self._funded_by: dict[UUID, UUID] = {}

    def fund_from(self, leaf_contract_id: UUID, account_contract_id: UUID) -> None:
        self._funded_by[leaf_contract_id] = account_contract_id

    async def put(
        self,
        contract_id: UUID,
        slot: CredentialSlot,
        *,
        evidence: Evidence | None = None,
    ) -> SlotProbe:
        """Probe a delivered value and swap it in only if the provider accepts it.

        Refusing keeps the value already in service. The alternative — trusting delivery
        and discovering the key is dead on the next call — strands every Directive on the
        Contract, which is the failure 22 A10 asks the probe to prevent.
        """

        if not await self._probe(slot):
            self._probes[contract_id] = SlotProbe.INVALID
            if evidence is not None:
                await evidence(
                    SLOT_REFUSED_SOURCE,
                    {
                        "reason": "probe_invalid",
                        "contract_id": str(contract_id),
                        "runtime_kind": slot.runtime_kind,
                        "key_id": slot.key_id,
                        "reference": slot.reference,
                        "probe": SlotProbe.INVALID.value,
                    },
                )
            return SlotProbe.INVALID
        self._slots[contract_id] = slot
        self._probes[contract_id] = SlotProbe.VALID
        if evidence is not None:
            await evidence(
                SLOT_SWAPPED_SOURCE,
                {
                    "contract_id": str(contract_id),
                    "runtime_kind": slot.runtime_kind,
                    "key_id": slot.key_id,
                    "reference": slot.reference,
                    "probe": SlotProbe.VALID.value,
                },
            )
        return SlotProbe.VALID

    def drop(self, contract_id: UUID) -> None:
        """Forget a Contract's slot: wiped, not listed (22 A9). The next call is refused."""

        self._slots.pop(contract_id, None)
        self._probes.pop(contract_id, None)
        self._last_used.pop(contract_id, None)

    def resolve(self, contract_id: UUID | None) -> CredentialSlot | None:
        """The slot this call spends, read at the call (22 A8)."""

        if contract_id is None:
            return None
        slot = self._slots.get(contract_id)
        if slot is None:
            funder = self._funded_by.get(contract_id)
            if funder is not None:
                slot = self._slots.get(funder)
        if slot is not None:
            self._last_used[contract_id] = datetime.now(UTC)
        return slot

    def holds(self, contract_id: UUID) -> bool:
        """Whether a call for this Contract would find a slot -- without the use stamp
        :meth:`resolve` leaves, because choosing a Directive's mode spends nothing."""

        funder = self._funded_by.get(contract_id)
        return contract_id in self._slots or (funder is not None and funder in self._slots)

    def statuses(self) -> list[SlotStatus]:
        """The heartbeat's slot fields (22 A10, issue 31's ``valid|invalid|unprobed``)."""

        contract_ids = sorted(set(self._slots) | set(self._probes) | set(self._funded_by), key=str)
        return [
            SlotStatus(
                contract_id=contract_id,
                runtime_kind=(
                    self._slots[contract_id].runtime_kind if contract_id in self._slots else "codex"
                ),
                present=contract_id in self._slots,
                key_id=(self._slots[contract_id].key_id if contract_id in self._slots else None),
                delivered_at=(
                    self._slots[contract_id].delivered_at if contract_id in self._slots else None
                ),
                last_used_at=self._last_used.get(contract_id),
                probe=self._probes.get(contract_id, SlotProbe.UNPROBED),
            )
            for contract_id in contract_ids
        ]


@dataclass(frozen=True, slots=True)
class Ceilings:
    """One Contract's monthly ceilings as the control plane last stated them (12 B4).

    ``used`` is the control plane's figure at the moment of the push; the Runner adds
    what it has metered since. Pushing again replaces both, so a raised ceiling — or a
    corrected total — applies at the very next call.
    """

    contract_limit: int | None = None
    contract_used: int = 0
    organisation_limit: int | None = None
    organisation_used: int = 0
    # The Organisation's ceiling covers its org-funded Contracts only: a user-funded
    # Contract spends the user's own key and is none of the org's budget (map 12 B3).
    org_funded: bool = False


class CeilingStore:
    """What each Contract may still spend, and what it has spent here since the push."""

    def __init__(self) -> None:
        self._ceilings: dict[UUID, Ceilings] = {}
        self._spent: dict[UUID, int] = {}

    def push(self, contract_id: UUID, ceilings: Ceilings) -> None:
        self._ceilings[contract_id] = ceilings
        self._spent[contract_id] = 0

    def spend(self, contract_id: UUID | None, tokens: int) -> None:
        if contract_id is None or tokens <= 0:
            return
        self._spent[contract_id] = self._spent.get(contract_id, 0) + tokens

    def authorize(self, contract_id: UUID | None) -> None:
        """Refuse the call the month can no longer fund, before any provider is touched.

        Checked against the month to date: a call may overshoot its ceiling by its own
        spend and no more, the same bound the Work Record Budget carries between
        Directives. A Contract with no pushed ceiling is unbounded here — the control
        plane is the authority on limits, and inventing one would refuse work nobody
        capped.
        """

        ceilings = self._ceilings.get(contract_id) if contract_id is not None else None
        if ceilings is None:
            return
        local = self._spent.get(contract_id, 0) if contract_id is not None else 0
        if ceilings.contract_limit is not None:
            used = ceilings.contract_used + local
            if used >= ceilings.contract_limit:
                raise CeilingExhaustedError(
                    ceiling="contract", limit=ceilings.contract_limit, used=used
                )
        if not ceilings.org_funded or ceilings.organisation_limit is None:
            return
        used = ceilings.organisation_used + local
        if used >= ceilings.organisation_limit:
            raise CeilingExhaustedError(
                ceiling="organisation", limit=ceilings.organisation_limit, used=used
            )


class UsageOutbox:
    """Usage Records waiting for the heartbeat that carries them out (ADR-0013 §11).

    At-least-once by construction: a record stays here until an acknowledgement names
    it, so a dropped ack costs a re-send. ``(directive_id, sequence)`` is what the ledger
    de-duplicates on, so the re-send lands on the row it already wrote.
    """

    def __init__(self, *, capacity: int = 10_000) -> None:
        self._capacity = capacity
        self._pending: dict[str, UsageRecord] = {}
        self._sequences: dict[str, int] = {}

    def next_sequence(self, directive_id: str) -> int:
        # ponytail: one counter per `directive_id`, kept for the process's life and never
        # pruned. A restart resets them, so a Directive still running resumes at 0 and the
        # ledger reads the new rows as duplicates of the ones it already committed --
        # the idempotency key becoming a data-loss key. The upgrade path is a
        # restart-unique prefix (the Runner's registration id) on the key, which changes
        # the wire shape both ends de-duplicate on and so belongs with the heartbeat loop.
        sequence = self._sequences.get(directive_id, 0)
        self._sequences[directive_id] = sequence + 1
        return sequence

    def record(self, record: UsageRecord) -> None:
        if len(self._pending) >= self._capacity and record.key not in self._pending:
            # Oldest first: a Runner that cannot reach the control plane for long enough
            # to fill this has a bigger problem than the tail of its own ledger, and
            # unbounded growth would take the process down with it.
            self._pending.pop(next(iter(self._pending)))
        self._pending[record.key] = record

    def pending(self, limit: int = USAGE_BATCH_MAX) -> list[UsageRecord]:
        return list(self._pending.values())[:limit]

    def acknowledge(self, keys: list[str]) -> int:
        return sum(1 for key in keys if self._pending.pop(key, None) is not None)

    def __len__(self) -> int:
        return len(self._pending)


@dataclass(frozen=True, slots=True)
class AttemptHandle:
    """What one Directive attempt is given: a URL under its own path, and its bearer."""

    attempt_id: str
    token: str
    base_url: str
    directive_id: str
    contract_id: UUID | None
    agent_id: UUID | None
    work_record_id: UUID | None
    # A per-attempt cap on top of the Contract's ceilings: the Learning reserve (PRD issue
    # 55, map 12 B6). None is no cap -- every ordinary Directive.
    reserve_max_tokens: int | None = None

    def env(self, cli_kind: str) -> dict[str, str]:
        return attempt_env(cli_kind, base_url=self.base_url, token=self.token)


def attempt_env(cli_kind: str, *, base_url: str, token: str) -> dict[str, str]:
    """The names each harness reads its endpoint and credential from.

    The "credential" here is the attempt's own bearer, which reaches only this Runner's
    loopback proxy and dies with the attempt — so ADR-0011 §9 still holds: the subprocess
    has no provider key, and anything it sends is metered and ceiling-checked on the way
    out.
    """

    if cli_kind.startswith("claude"):
        # Claude Code appends `/v1/messages` to what it is given; the OpenAI-compatible
        # CLIs are configured with the `/v1` already on. Same attempt, same bearer — only
        # the half of the URL each harness expects to supply differs.
        return {"ANTHROPIC_BASE_URL": base_url, "ANTHROPIC_AUTH_TOKEN": token}
    return {PROXY_BASE_URL_ENV: f"{base_url}/v1", PROXY_API_KEY_ENV: token}


class LlmProxy:
    """One Runner process's proxy: loopback TCP, per-attempt bearer, metered per call."""

    def __init__(
        self,
        *,
        slots: SlotStore,
        ceilings: CeilingStore | None = None,
        outbox: UsageOutbox | None = None,
        prices: dict[str, ModelPrice] | None = None,
        evidence: Evidence | None = None,
        client: httpx.AsyncClient | None = None,
        host: str = "127.0.0.1",
        port: int = 0,
    ) -> None:
        if host not in _LOOPBACK_HOSTS:
            # The proxy resolves the funder's key on every request. Its only legitimate
            # callers are subprocesses of this same process, so a bind anyone else can
            # reach is refused here rather than left to a network policy to catch.
            raise ValueError(f"the LLM proxy binds loopback only, not {host!r}")
        self.slots = slots
        self.ceilings = ceilings or CeilingStore()
        self.outbox = outbox or UsageOutbox()
        self._prices = prices or {}
        self._evidence = evidence
        self._client = client
        self._owns_client = client is None
        self._host = host
        self._port = port
        self._server: asyncio.AbstractServer | None = None
        self._attempts: dict[str, AttemptHandle] = {}
        # Tokens each capped attempt has spent, keyed by attempt id; gone with the attempt.
        self._reserve_spent: dict[str, int] = {}

    @property
    def base_url(self) -> str:
        return f"http://{self._host}:{self._port}"

    async def __aenter__(self) -> Self:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0))
        self._server = await asyncio.start_server(self._serve, host=self._host, port=self._port)
        # Port 0 by default: the operator configures no port for a surface only this
        # process's own children ever dial.
        self._port = self._server.sockets[0].getsockname()[1]
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
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    @contextlib.asynccontextmanager
    async def attempt(
        self,
        *,
        directive_id: str,
        contract_id: UUID | None,
        agent_id: UUID | None = None,
        work_record_id: UUID | None = None,
        token: str | None = None,
        reserve_max_tokens: int | None = None,
    ) -> AsyncIterator[AttemptHandle]:
        """Admit one Directive attempt for as long as it runs, and no longer.

        ``token`` is the attempt's callback bearer (issue 45) when there is one: one
        token per attempt serves both surfaces, so the subprocess holds exactly one
        secret and it expires with the attempt either way.
        """

        attempt_id = secrets.token_urlsafe(9)
        handle = AttemptHandle(
            attempt_id=attempt_id,
            token=token or secrets.token_urlsafe(_TOKEN_BYTES),
            base_url=f"{self.base_url}{ATTEMPT_PREFIX}{attempt_id}",
            directive_id=directive_id,
            contract_id=contract_id,
            agent_id=agent_id,
            work_record_id=work_record_id,
            reserve_max_tokens=reserve_max_tokens,
        )
        self._attempts[attempt_id] = handle
        try:
            yield handle
        finally:
            self._attempts.pop(attempt_id, None)
            self._reserve_spent.pop(attempt_id, None)

    # ----------------------------------------------------------------- serving

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await self._route(reader, writer)
        except Exception as error:  # noqa: BLE001 - one bad call never takes the proxy down
            await write_json(writer, 500, {"detail": error.__class__.__name__})
        finally:
            writer.close()
            with contextlib.suppress(ConnectionResetError, BrokenPipeError):
                await writer.wait_closed()

    async def _route(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request = await read_request(reader, max_body_bytes=MAX_REQUEST_BODY_BYTES)
        if not isinstance(request, HttpRequest):
            await write_json(writer, *request)
            return

        attempt, path = self._authorise(request)
        if attempt is None:
            # One answer for an unknown attempt and for a wrong bearer: which of the two
            # it was is not the caller's business.
            await write_json(writer, 401, {"detail": "a valid attempt bearer is required"})
            return

        # The route is checked before the slot is resolved: an unknown path is not a
        # funding failure, and answering one with a 503 and an Evidence Event would let a
        # mistyped URL write Evidence.
        models_list = request.method == "GET" and path == "/models"
        if not models_list and (request.method != "POST" or path not in COMPLETION_PATHS):
            await write_json(writer, 404, {"detail": "no such proxy route"})
            return

        slot = self.slots.resolve(attempt.contract_id)
        if slot is None:
            await self._refused(attempt, reason="slot_missing")
            await write_json(
                writer, 503, {"error": {"message": "no LLM slot", "type": "slot_unavailable"}}
            )
            return

        if models_list:
            await self._relay_models(writer, slot=slot)
            return

        try:
            self.ceilings.authorize(attempt.contract_id)
        except CeilingExhaustedError as error:
            await self._refused(attempt, reason="ceiling_exhausted", ceiling=error.ceiling)
            await write_json(writer, 402, ceiling_exhausted_detail(error))
            return

        spent = self._reserve_spent.get(attempt.attempt_id, 0)
        if attempt.reserve_max_tokens is not None and spent >= attempt.reserve_max_tokens:
            await self._refused(attempt, reason="reserve_exhausted", used=spent)
            await write_json(
                writer,
                402,
                {
                    "error": {
                        "message": (
                            f"reserve exhausted: {spent} of {attempt.reserve_max_tokens} "
                            "tokens spent"
                        ),
                        "type": "reserve_exhausted",
                    }
                },
            )
            return

        await self._relay_completion(writer, request=request, attempt=attempt, slot=slot, path=path)

    def _authorise(self, request: HttpRequest) -> tuple[AttemptHandle | None, str]:
        """Which attempt this request is, by path *and* bearer.

        The path segment is what makes another attempt's bearer a refusal rather than a
        mis-attribution: a token that is valid for some other live attempt does not open
        this one.
        """

        if not request.target.startswith(ATTEMPT_PREFIX):
            return None, ""
        attempt_id, _, rest = request.target[len(ATTEMPT_PREFIX) :].partition("/")
        attempt = self._attempts.get(attempt_id)
        if attempt is None or not bearer_matches(request.authorization, attempt.token):
            return None, ""
        # The `/v1` is the harness's, not ours: Claude Code appends it and the
        # OpenAI-compatible CLIs carry it in the base URL they were configured with.
        path = "/" + rest.removeprefix("v1/").lstrip("/")
        return attempt, path.partition("?")[0]

    async def _relay_models(self, writer: asyncio.StreamWriter, *, slot: CredentialSlot) -> None:
        assert self._client is not None
        try:
            upstream = await self._client.get(slot.url("models"), headers=slot.headers())
        except httpx.HTTPError as error:
            await write_json(
                writer,
                502,
                {"error": {"message": "provider unreachable", "type": error.__class__.__name__}},
            )
            return
        writer.write(
            response_head(
                upstream.status_code,
                content_type="application/json",
                content_length=len(upstream.content),
            )
        )
        writer.write(upstream.content)
        with contextlib.suppress(ConnectionResetError, BrokenPipeError):
            await writer.drain()

    async def _relay_completion(
        self,
        writer: asyncio.StreamWriter,
        *,
        request: HttpRequest,
        attempt: AttemptHandle,
        slot: CredentialSlot,
        path: str,
    ) -> None:
        assert self._client is not None
        payload = _json_object(request.body)
        model_name = str(payload.get("model") or "unknown")
        streaming = payload.get("stream") is True
        url = slot.url(path)
        headers = _upstream_headers(request, slot)
        body = request.body
        if streaming and path == "/chat/completions":
            # An OpenAI-compatible endpoint omits the usage block from a stream unless it
            # is asked for, and a streamed call metered at zero is a ledger row missing
            # its dominant term and a per-call ceiling that cannot be enforced. Anthropic
            # sends the counts either way, split across two frames (`_sse_usage`).
            body = _with_usage_in_stream(payload)
        started = asyncio.get_running_loop().time()

        if not streaming:
            try:
                upstream = await self._client.post(url, content=body, headers=headers)
            except httpx.HTTPError as error:
                self._meter(
                    attempt,
                    slot=slot,
                    model_name=model_name,
                    usage={},
                    started=started,
                    error_class=error.__class__.__name__,
                )
                await write_json(
                    writer,
                    502,
                    {"error": {"message": "provider unreachable", "type": "provider_error"}},
                )
                return
            self._meter(
                attempt,
                slot=slot,
                model_name=model_name,
                usage=_usage_block(_json_object(upstream.content)),
                started=started,
                error_class=None if upstream.status_code < 400 else f"http_{upstream.status_code}",
            )
            writer.write(
                response_head(
                    upstream.status_code,
                    content_type="application/json",
                    content_length=len(upstream.content),
                )
            )
            writer.write(upstream.content)
            with contextlib.suppress(ConnectionResetError, BrokenPipeError):
                await writer.drain()
            return

        usage: dict[str, object] = {}
        error_class: str | None = None
        started_relaying = False
        tail = b""
        try:
            async with self._client.stream("POST", url, content=body, headers=headers) as upstream:
                writer.write(response_head(upstream.status_code, content_type="text/event-stream"))
                started_relaying = True
                # The same code the non-streaming branch records: a streamed call the
                # provider refused meters no tokens, and a row with none and no error
                # class reads as a successful free call.
                if upstream.status_code >= 400:
                    error_class = f"http_{upstream.status_code}"
                async for chunk in upstream.aiter_bytes():
                    writer.write(chunk)
                    # Lines are re-assembled across chunk boundaries: a `data:` frame the
                    # transport split in two carries its counts in neither half.
                    *lines, tail = (tail + chunk).split(b"\n")
                    usage.update(_sse_usage(lines))
                    with contextlib.suppress(ConnectionResetError, BrokenPipeError):
                        await writer.drain()
        except httpx.HTTPError as error:
            error_class = error.__class__.__name__
        usage.update(_sse_usage([tail]))
        self._meter(
            attempt,
            slot=slot,
            model_name=model_name,
            usage=usage,
            started=started,
            error_class=error_class,
        )
        if error_class is not None and not started_relaying:
            # Nothing has been written yet, so the caller can still be told why rather
            # than being handed a connection that simply closes.
            await write_json(
                writer,
                502,
                {"error": {"message": "provider unreachable", "type": "provider_error"}},
            )

    # ----------------------------------------------------------------- metering

    def _meter(
        self,
        attempt: AttemptHandle,
        *,
        slot: CredentialSlot,
        model_name: str,
        usage: Mapping[str, object],
        started: float,
        error_class: str | None,
    ) -> None:
        """One Usage Record per call, and the tokens the ceiling now counts.

        Reads the upstream ``usage`` block and nothing else — the completion itself is
        forwarded and forgotten, which is the rule the structural test pins.
        """

        prompt_tokens = _prompt_tokens(usage)
        completion_tokens = _completion_tokens(usage)
        price = resolve_price(self._prices, provider_name=slot.provider_name, model_name=model_name)
        record = UsageRecord(
            directive_id=attempt.directive_id,
            sequence=self.outbox.next_sequence(attempt.directive_id),
            provider_name=slot.provider_name,
            model_name=model_name,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cached_tokens=_cached_tokens(usage),
            applied_prompt_price_usd=price.prompt_usd_per_token,
            applied_completion_price_usd=price.completion_usd_per_token,
            cost_usd=price.cost_usd(
                prompt_tokens=prompt_tokens or 0, completion_tokens=completion_tokens or 0
            ),
            latency_ms=max(0, int((asyncio.get_running_loop().time() - started) * 1000)),
            error_class=error_class,
            agent_id=attempt.agent_id,
            contract_id=attempt.contract_id,
            work_record_id=attempt.work_record_id,
        )
        self.outbox.record(record)
        tokens = (prompt_tokens or 0) + (completion_tokens or 0)
        self.ceilings.spend(attempt.contract_id, tokens)
        if attempt.reserve_max_tokens is not None:
            self._reserve_spent[attempt.attempt_id] = (
                self._reserve_spent.get(attempt.attempt_id, 0) + tokens
            )

    async def _refused(self, attempt: AttemptHandle, *, reason: str, **extra: object) -> None:
        """A refused call is an Evidence Event with the reason — ids only (issue 43)."""

        if self._evidence is None:
            return
        await self._evidence(
            CALL_REFUSED_SOURCE,
            {
                "reason": reason,
                "directive_id": attempt.directive_id,
                "contract_id": str(attempt.contract_id) if attempt.contract_id else None,
                "agent_id": str(attempt.agent_id) if attempt.agent_id else None,
                **extra,
            },
        )


def _json_object(raw: bytes) -> dict[str, Any]:
    try:
        parsed = json.loads(raw or b"{}")
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _upstream_headers(request: HttpRequest, slot: CredentialSlot) -> dict[str, str]:
    """The harness's own headers, with the slot's credential in place of its bearer.

    Forwarded rather than rebuilt: ``anthropic-beta`` is how Claude Code asks for the
    features it is built against, and a proxy that drops it answers a different call than
    the one the Agent made.
    """

    forwarded = {
        name: value for name, value in request.headers.items() if name not in _HEADERS_NOT_FORWARDED
    }
    # The harness's headers win over the slot's: the credential cannot be among them (both
    # auth headers are stripped above), and a harness that asks for a newer
    # `anthropic-version` than the slot's floor must get the wire it asked for.
    return {"content-type": "application/json", **slot.headers(), **forwarded}


def _with_usage_in_stream(payload: dict[str, Any]) -> bytes:
    """``stream_options.include_usage``, without discarding one the caller already set."""

    options = payload.get("stream_options")
    payload["stream_options"] = {
        **(options if isinstance(options, Mapping) else {}),
        "include_usage": True,
    }
    return json.dumps(payload).encode()


def _usage_block(body: Mapping[str, object]) -> Mapping[str, object]:
    usage = body.get("usage")
    if isinstance(usage, Mapping):
        return usage
    # Anthropic's `message_start` frame nests the input counts one level down, under the
    # message it is starting, and the Responses API's `response.completed` under the
    # response it completes. Each is followed only to reach that `usage` block -- nothing
    # else in the frame is read (ADR-0010 §4).
    for inner in (body.get("message"), body.get("response")):
        if isinstance(inner, Mapping):
            nested = inner.get("usage")
            if isinstance(nested, Mapping):
                return nested
    return {}


def _sse_usage(lines: Iterable[bytes]) -> dict[str, object]:
    """The ``usage`` counts these SSE lines carried, merged rather than replaced.

    One call's counts can arrive on more than one frame: Anthropic puts the input tokens
    on ``message_start`` and the output tokens on ``message_delta``. Keeping only the last
    block seen would meter a streamed ``/v1/messages`` call with no prompt tokens at all
    -- the dominant term of both the ledger row and the ceiling -- and Claude Code streams
    by default, so that is the normal path for one of the two harnesses.
    """

    found: dict[str, object] = {}
    for raw in lines:
        line = raw.strip()
        if not line.startswith(b"data:") or line.startswith(_SSE_DONE):
            continue
        found.update(_usage_block(_json_object(line[len(b"data:") :].strip())))
    return found


def _usage_int(usage: Mapping[str, object], key: str) -> int | None:
    value = usage.get(key)
    return value if isinstance(value, int) else None


def _first_int(usage: Mapping[str, object], *keys: str) -> int | None:
    """The first of ``keys`` this usage block carries.

    Two wires, one Usage Record: OpenAI-compatible endpoints say ``prompt_tokens`` and
    Anthropic says ``input_tokens``. The ledger and the ceilings count tokens, so the
    difference is a spelling and is resolved here rather than in the row.
    """

    for key in keys:
        value = _usage_int(usage, key)
        if value is not None:
            return value
    return None


def _prompt_tokens(usage: Mapping[str, object]) -> int | None:
    tokens = _first_int(usage, "prompt_tokens", "input_tokens")
    if tokens is None:
        return None
    # Cache *writes* are new input the model had to encode, so they are prompt tokens —
    # the same split `claude_code_harness_usage` already prices (PRD issue 31).
    return tokens + (_usage_int(usage, "cache_creation_input_tokens") or 0)


def _completion_tokens(usage: Mapping[str, object]) -> int | None:
    return _first_int(usage, "completion_tokens", "output_tokens")


def _cached_tokens(usage: Mapping[str, object]) -> int | None:
    """Cached input, wherever the provider puts it. Counted at full weight (map 12 B1)."""

    direct = _first_int(usage, "cached_tokens", "cache_read_input_tokens")
    if direct is not None:
        return direct
    for key in ("prompt_tokens_details", "input_tokens_details"):
        details = usage.get(key)
        if isinstance(details, Mapping):
            return _usage_int(details, "cached_tokens")
    return None
