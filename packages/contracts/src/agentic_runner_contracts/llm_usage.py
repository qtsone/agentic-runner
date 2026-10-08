"""Metering shapes shared by the platform and the Runner (PRD issues 13 and 43).

Issue 13 built the Usage Record and the ceiling semantics against the backend proxy;
issue 43 moves the metering point onto the host that holds the funder's key. The move is
only a move if both ends keep spelling the same things, so the price table, the typed
ceiling refusal and the Usage Record itself live here rather than once on each side —
the platform's ``services.usage`` re-exports them, and the Runner's ``llm_proxy`` imports
them directly.

A Usage Record carries counts, prices and ids. Never a prompt, never a completion: the
upstream ``usage`` block is the only part of a provider response the metering point
reads, which is what keeps ADR-0010 §4 true after the proxy leaves the control plane.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

__all__ = [
    "ZERO_PRICE",
    "CeilingExhaustedError",
    "CeilingKind",
    "ModelPrice",
    "UsageRecord",
    "ceiling_exhausted_detail",
    "price_table",
    "resolve_price",
]

CeilingKind = Literal["contract", "organisation", "chain"]


@dataclass(frozen=True)
class ModelPrice:
    """The per-token prices applied to one call. ~1e-6 USD each, hence Decimal."""

    prompt_usd_per_token: Decimal
    completion_usd_per_token: Decimal

    def cost_usd(self, *, prompt_tokens: int, completion_tokens: int) -> Decimal:
        return self.prompt_usd_per_token * prompt_tokens + (
            self.completion_usd_per_token * completion_tokens
        )


ZERO_PRICE = ModelPrice(prompt_usd_per_token=Decimal(0), completion_usd_per_token=Decimal(0))


def resolve_price(
    prices: dict[str, ModelPrice],
    *,
    provider_name: str,
    model_name: str,
) -> ModelPrice:
    """The price this call pays, from a table the Contract has already overridden.

    Keys are ``provider/model`` narrowing to ``*/model``, so an entry can be pinned to one
    provider of a model served by several. An unpriced model still meters its tokens — the
    ceilings are in tokens — and simply contributes nothing to the currency estimate.
    """

    for key in (f"{provider_name}/{model_name}", f"*/{model_name}"):
        price = prices.get(key)
        if price is not None:
            return price
    return ZERO_PRICE


def price_table(entries: list[dict[str, object]]) -> dict[str, ModelPrice]:
    """Build a price lookup from ``[{provider, model, prompt.., completion..}, ...]``."""

    table: dict[str, ModelPrice] = {}
    for entry in entries:
        model = entry.get("model")
        if not isinstance(model, str) or not model:
            continue
        provider = entry.get("provider")
        provider_key = provider if isinstance(provider, str) and provider else "*"
        prompt = _decimal(entry.get("prompt_usd_per_token"))
        completion = _decimal(entry.get("completion_usd_per_token"))
        if prompt is None or completion is None:
            continue
        table[f"{provider_key}/{model}"] = ModelPrice(
            prompt_usd_per_token=prompt,
            completion_usd_per_token=completion,
        )
    return table


def _decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (ArithmeticError, ValueError):
        return None


class CeilingExhaustedError(Exception):
    """A monthly token ceiling is spent. Refusal, never a fault (map ticket 12 B5)."""

    def __init__(self, *, ceiling: CeilingKind, limit: int, used: int) -> None:
        super().__init__(
            f"{ceiling} monthly token ceiling exhausted: {used} of {limit} tokens spent"
        )
        self.ceiling: CeilingKind = ceiling
        self.limit = limit
        self.used = used


def ceiling_exhausted_detail(error: CeilingExhaustedError) -> dict[str, object]:
    """The typed refusal body, spelled once (PRD issue 13, map ticket 12 B5).

    Carried at HTTP 402 rather than 429: nothing about retrying later this month changes
    the answer, and the ceiling is a funding fact, not a rate. Naming *which* ceiling is
    what lets the loop hold the one Work Record (Contract) or every org-funded one
    (Organisation) without a second round trip.
    """

    return {
        "error": {
            "message": str(error),
            "type": "ceiling_exhausted",
            "ceiling": error.ceiling,
            "limit": error.limit,
            "used": error.used,
        }
    }


class UsageRecord(BaseModel):
    """One metered call, as the Runner ships it outbound (ADR-0013 §11).

    ``(directive_id, sequence)`` is the idempotency key: the stream is at-least-once, so
    a record whose acknowledgement was dropped is re-sent and must land on the row it
    already wrote rather than beside it.

    No ``runner_id`` field, deliberately. It belongs on the ledger row (issue 13's keys)
    but the heartbeat is already signed by the Runner's identity, so the control plane
    fills it from what it verified instead of trusting a field a body could assert.
    """

    model_config = ConfigDict(extra="forbid")

    directive_id: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    sequence: int = Field(ge=0)
    provider_name: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    model_name: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    cached_tokens: int | None = Field(default=None, ge=0)
    applied_prompt_price_usd: Decimal | None = None
    applied_completion_price_usd: Decimal | None = None
    cost_usd: Decimal | None = None
    latency_ms: int = Field(default=0, ge=0)
    error_class: Annotated[str, StringConstraints(max_length=128)] | None = None
    agent_id: UUID | None = None
    contract_id: UUID | None = None
    work_record_id: UUID | None = None

    @property
    def key(self) -> str:
        """What an acknowledgement names, and what the ledger de-duplicates on."""

        return f"{self.directive_id}:{self.sequence}"

    @property
    def request_id(self) -> str:
        """The ledger's ``request_id`` for this record — its idempotency key, prefixed so
        it cannot collide with the proxy's per-request UUIDs or issue 31's harness rows."""

        return f"runner:{self.key}"
