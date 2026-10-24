"""The Triage Directive's one turn, on the Runner (PRD issue 29, issue 81).

Dispatched to ``runner.{runner_id}`` (the locality rule, ADR-0013 §2): the turn spends the
Intake Lead's Contract's LLM slot, which lives only in this Runner's proxy (ADR-0013 §4),
so the control plane -- which holds no Organisation LLM credential (ADR-0010 §4) -- only
prepares the prompt and dispatches the answer. The user-connected Sources' poller runs
the same turn in-process (``user_sources.ProxyTriage``).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from uuid import UUID

import httpx
from temporalio import activity

from agentic_runner.connections import contract_refusal
from agentic_runner.llm_proxy import LlmProxy
from agentic_runner_contracts.activity_io import TriageTurnInput, TriageTurnOutput
from agentic_runner_contracts.runner_registration import ContractLlmTerms

_logger = logging.getLogger(__name__)


async def run_triage_turn(
    proxy: LlmProxy,
    *,
    model: str,
    timeout_seconds: float,
    prompt: str,
    directive_id: str,
    contract_id: UUID | None,
    agent_id: UUID | None,
    reserve_max_tokens: int | None = None,
    delivered_only: bool = False,
    client: httpx.AsyncClient | None = None,
) -> str | None:
    """One completion through this Runner's proxy, metered like any Directive.

    ``None`` on anything that keeps the answer from being trusted -- a hang, a ceiling or
    Reserve refusal, a missing slot, a malformed body: the caller's fallback is the
    keyword classifier, never an Incident (PRD issue 29). ``delivered_only`` keeps the
    turn off this Runner's store key (``LlmProxy.attempt``).
    """

    body: dict[str, object] = {"model": model, "messages": [{"role": "user", "content": prompt}]}
    if reserve_max_tokens:
        # The proxy refuses only a call that starts with the Reserve spent, so one
        # completion could run past it; the turn is a fresh attempt, so all of the Reserve
        # is left (QTS-2455). `max_completion_tokens`, not `max_tokens`: OpenAI refuses the
        # older name on its reasoning models, the triage default among them.
        body["max_completion_tokens"] = reserve_max_tokens
    http = client or httpx.AsyncClient()
    try:
        async with proxy.attempt(
            directive_id=directive_id,
            contract_id=contract_id,
            agent_id=agent_id,
            reserve_max_tokens=reserve_max_tokens,
            delivered_only=delivered_only,
        ) as handle:
            response = await http.post(
                f"{handle.base_url}/chat/completions",
                json=body,
                headers={"authorization": f"Bearer {handle.token}"},
                timeout=timeout_seconds,
            )
        if response.status_code >= 400:
            return None
        return _content(response.json())
    except (httpx.HTTPError, ValueError):
        return None
    finally:
        if client is None:
            await http.aclose()


def _content(completion: object) -> str | None:
    if not isinstance(completion, dict):
        return None
    choices = completion.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return None
    message = choices[0].get("message")
    content = message.get("content") if isinstance(message, dict) else None
    return content if isinstance(content, str) else None


class RunnerTriageActivities:
    def __init__(
        self,
        *,
        proxy: LlmProxy,
        model: str,
        timeout_seconds: float,
        host_party: str | None = None,
        llm_contracts: Callable[[], Sequence[ContractLlmTerms] | None] | None = None,
        client: httpx.AsyncClient | None = None,
        connection_organisation: Callable[[], UUID | None] | None = None,
    ) -> None:
        self._proxy = proxy
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._host_party = host_party
        self._llm_contracts = llm_contracts
        self._client = client
        # As on the Ralph activities: a connection spends only its own Organisation's
        # Contracts' LLM slots (Spec: Runners rev 14).
        self._connection_organisation = connection_organisation

    def activity_callables(self) -> list[Callable[..., object]]:
        return [self.run_triage_turn]

    @activity.defn(name="run_triage_turn")
    async def run_triage_turn(self, request: TriageTurnInput) -> TriageTurnOutput:
        contract_id = _optional_uuid(request.intake_lead_contract_id)
        if (refusal := self._connection_refusal(contract_id)) is not None:
            # No answer is the turn's own failure mode: the keyword classifier decides.
            _logger.warning("triage turn refused: %s for Contract %s", refusal, contract_id)
            return TriageTurnOutput(raw_text=None)
        raw_text = await run_triage_turn(
            self._proxy,
            model=self._model,
            timeout_seconds=self._timeout_seconds,
            prompt=request.prompt,
            directive_id=request.directive_id,
            contract_id=contract_id,
            agent_id=_optional_uuid(request.intake_lead_agent_id),
            reserve_max_tokens=request.reserve_max_tokens,
            delivered_only=self._pool_runner_before_named_access(),
            client=self._client,
        )
        return TriageTurnOutput(raw_text=raw_text)

    def _connection_refusal(self, contract_id: UUID | None) -> str | None:
        organisation = self._connection_organisation() if self._connection_organisation else None
        if organisation is None:
            return None
        terms = (self._llm_contracts() if self._llm_contracts is not None else None) or ()
        return contract_refusal(organisation, terms, contract_id)

    def _pool_runner_before_named_access(self) -> bool:
        """A pool Runner's store key stays out of the turn until an ack names the
        Contracts: a control plane below 3.25 caps none of that spend, the rule a
        Directive that names no access follows (QTS-1736, QTS-1751)."""

        named = self._llm_contracts() if self._llm_contracts is not None else None
        return self._host_party != "user" and named is None


def _optional_uuid(value: str) -> UUID | None:
    try:
        return UUID(value)
    except ValueError:
        return None
