"""The Triage Directive's one turn, on the Runner (PRD issue 29, issue 81).

Dispatched to ``runner.{runner_id}`` (the locality rule, ADR-0013 §2): the turn spends the
Intake Lead's Contract's LLM slot, which lives only in this Runner's proxy (ADR-0013 §4),
so the control plane -- which holds no Organisation LLM credential (ADR-0010 §4) -- only
prepares the prompt and dispatches the answer. The user-connected Sources' poller runs
the same turn in-process (``user_sources.ProxyTriage``).
"""

from __future__ import annotations

from collections.abc import Callable
from uuid import UUID

import httpx
from temporalio import activity

from agentic_runner.llm_proxy import LlmProxy
from agentic_runner_contracts.activity_io import TriageTurnInput, TriageTurnOutput


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
    client: httpx.AsyncClient | None = None,
) -> str | None:
    """One completion through this Runner's proxy, metered like any Directive.

    ``None`` on anything that keeps the answer from being trusted -- a hang, a ceiling or
    Reserve refusal, a missing slot, a malformed body: the caller's fallback is the
    keyword classifier, never an Incident (PRD issue 29).
    """

    http = client or httpx.AsyncClient()
    try:
        async with proxy.attempt(
            directive_id=directive_id,
            contract_id=contract_id,
            agent_id=agent_id,
            reserve_max_tokens=reserve_max_tokens,
        ) as handle:
            response = await http.post(
                f"{handle.base_url}/chat/completions",
                json={"model": model, "messages": [{"role": "user", "content": prompt}]},
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
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._proxy = proxy
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._client = client

    def activity_callables(self) -> list[Callable[..., object]]:
        return [self.run_triage_turn]

    @activity.defn(name="run_triage_turn")
    async def run_triage_turn(self, request: TriageTurnInput) -> TriageTurnOutput:
        raw_text = await run_triage_turn(
            self._proxy,
            model=self._model,
            timeout_seconds=self._timeout_seconds,
            prompt=request.prompt,
            directive_id=request.directive_id,
            contract_id=_optional_uuid(request.intake_lead_contract_id),
            agent_id=_optional_uuid(request.intake_lead_agent_id),
            reserve_max_tokens=request.reserve_max_tokens,
            client=self._client,
        )
        return TriageTurnOutput(raw_text=raw_text)


def _optional_uuid(value: str) -> UUID | None:
    try:
        return UUID(value)
    except ValueError:
        return None
