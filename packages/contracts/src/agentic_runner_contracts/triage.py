"""The Triage Directive's decision: structured LLM output, or the keyword fallback (PRD issue 29).

A contract rather than platform code since PRD issue 50: a user-connected Source is triaged
on the workstation Runner that reads it, so the prompt, the parse and the never-guess
fallback have to be the same bytes on both sides of the boundary.

One turn, single-label output (``own | ask | ignore`` plus ``lead_agent_id`` and a reason).
This module is pure -- no I/O -- so both the dispatch endpoint and its tests read the same
decision logic: parse the runtime's structured output and validate it against the pool of
candidate Leads; a hanging, malformed, or hallucinated-``lead_agent_id`` answer falls back
to the keyword classifier (:func:`classify_specialisation`), which itself never
guesses -- unclassifiable text is always ``ask``, never ``own`` (the never-guess invariant,
double ambiguity is ``ask``).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

# Deterministic specialisation signals. Routing is privileged, so the classifier only
# commits when exactly one specialisation's signal is present; no signal, or more than
# one, is ambiguous and defers to a human.
_REVIEW_SIGNALS = ("review", "pull request", " pr ", "pr #", "approve the", "code review")
_OPS_SIGNALS = (
    "deploy",
    "rollback",
    "roll back",
    "restart",
    "scale",
    "rollout",
    "runbook",
    "incident",
    "provision",
)
_DEVELOPMENT_SIGNALS = (
    "implement",
    "fix",
    "add ",
    "build",
    "refactor",
    "bug",
    "feature",
    "create",
    "write",
    "update",
    "change",
    "patch",
)

Specialisation = Literal["development", "review", "ops"]


def classify_specialisation(text: str) -> Specialisation | None:
    """Classify a request to one Agent Specialisation, or ``None`` if ambiguous.

    Review and ops are specialised signals that dominate the generic development
    vocabulary (a "review" or "deploy" request is not made ambiguous by also containing a
    word like "change"). Commit only when a single specialisation is implied: conflicting
    specialised signals (review *and* ops), or no recognizable signal at all, is ambiguous
    and must default to a human rather than be guessed.
    """
    haystack = f" {text.lower()} "
    specialised: list[Specialisation] = []
    if _has_signal(haystack, _REVIEW_SIGNALS):
        specialised.append("review")
    if _has_signal(haystack, _OPS_SIGNALS):
        specialised.append("ops")
    if len(specialised) > 1:
        return None
    if len(specialised) == 1:
        return specialised[0]
    if _has_signal(haystack, _DEVELOPMENT_SIGNALS):
        return "development"
    return None


def _has_signal(haystack: str, signals: tuple[str, ...]) -> bool:
    return any(signal in haystack for signal in signals)


TriageOutcome = Literal["own", "ask", "ignore"]
TriageSource = Literal["llm", "fallback"]

_VALID_OUTCOMES = frozenset({"own", "ask", "ignore"})
_DEFAULT_REASON = "Triage Directive decision."


@dataclass(frozen=True)
class TriageCandidate:
    """One candidate Lead: an Agent deployed under an active, Product-scoped Contract."""

    agent_id: UUID
    specialisation: str


@dataclass(frozen=True)
class TriageDecision:
    outcome: TriageOutcome
    lead_agent_id: UUID | None
    reason: str
    source: TriageSource


class TriageDirectiveParseError(ValueError):
    """Raised when the runtime's output cannot be trusted as a decision.

    Never surfaced to a caller: it is the signal that triggers the keyword fallback,
    caught by :func:`decide` alone.
    """


def build_prompt(
    *,
    message_text: str,
    intake_brief: str | None,
    candidates: Sequence[TriageCandidate],
) -> str:
    """The Directive's prompt: the message, the optional Brief, and the candidate pool.

    Structured-output instructions are explicit rather than relying on a response format
    the fallback provider may not honour -- the JSON contract is part of the prompt, and a
    reply that does not follow it is exactly what :func:`parse_llm_decision` refuses.
    """

    lines = [
        "You are the Triage Directive for a Product's Intake Lead. Classify the message "
        "below as exactly one of: own, ask, ignore.",
        "- own: this is real work you can hand to one of the candidate Leads listed below.",
        "- ask: the message is ambiguous or you cannot tell which Lead should own it.",
        "- ignore: the message is not a request at all (chatter, an ack, off-topic).",
        'Reply with one JSON object only: {"outcome": "own|ask|ignore", '
        '"lead_agent_id": "<uuid or null>", "reason": "<one line>"}. '
        "lead_agent_id is required and must be one of the candidate ids when outcome is "
        "own; null otherwise. Never guess a Lead you are not sure of -- reply ask instead.",
    ]
    if intake_brief:
        lines.append(f"Intake Brief: {intake_brief}")
    if candidates:
        lines.append(
            "Candidate Leads: "
            + ", ".join(f"{c.agent_id} ({c.specialisation})" for c in candidates)
        )
    else:
        lines.append("Candidate Leads: none currently deployed.")
    lines.append(f"Message: {message_text}")
    return "\n".join(lines)


def parse_llm_decision(
    raw_text: str | None,
    *,
    candidates: Sequence[TriageCandidate],
) -> TriageDecision:
    """Parse and validate the runtime's structured output, or raise.

    Every way an answer can fail to be trusted is one error: absent (a hang or a runtime
    error), not JSON, not an object, an unrecognised outcome, or an ``own`` whose
    ``lead_agent_id`` is missing, malformed, or not in the candidate pool (a hallucinated
    id). Any of these is treated exactly like a malformed answer -- fall back.
    """

    if raw_text is None or not raw_text.strip():
        raise TriageDirectiveParseError("no runtime output")
    try:
        payload = json.loads(raw_text)
    except (TypeError, ValueError) as error:
        raise TriageDirectiveParseError(f"malformed JSON: {error}") from error
    if not isinstance(payload, dict):
        raise TriageDirectiveParseError("output is not a JSON object")

    outcome = payload.get("outcome")
    if outcome not in _VALID_OUTCOMES:
        raise TriageDirectiveParseError(f"unrecognised outcome {outcome!r}")

    reason = payload.get("reason")
    reason_text = reason.strip() if isinstance(reason, str) and reason.strip() else _DEFAULT_REASON

    lead_agent_id: UUID | None = None
    if outcome == "own":
        raw_lead_id = payload.get("lead_agent_id")
        try:
            lead_agent_id = UUID(str(raw_lead_id))
        except (TypeError, ValueError) as error:
            raise TriageDirectiveParseError("own outcome missing a valid lead_agent_id") from error
        if lead_agent_id not in {candidate.agent_id for candidate in candidates}:
            raise TriageDirectiveParseError(
                f"lead_agent_id {lead_agent_id} is not a candidate Lead"
            )

    return TriageDecision(
        outcome=outcome, lead_agent_id=lead_agent_id, reason=reason_text, source="llm"
    )


def fallback_decision(text: str, *, candidates: Sequence[TriageCandidate]) -> TriageDecision:
    """The keyword classifier's fallback: ``own`` when it can place the work, else
    ``ask`` -- never ``ignore`` (PRD issue 29 acceptance: unclassifiable falls to a human,
    never guessed into silence)."""

    specialisation = classify_specialisation(text)
    if specialisation is not None:
        target = next(
            (candidate for candidate in candidates if candidate.specialisation == specialisation),
            None,
        )
        if target is not None:
            return TriageDecision(
                outcome="own",
                lead_agent_id=target.agent_id,
                reason=f"Keyword fallback matched {specialisation} work.",
                source="fallback",
            )
    return TriageDecision(
        outcome="ask",
        lead_agent_id=None,
        reason="Keyword fallback could not classify this request.",
        source="fallback",
    )


def decide(
    raw_text: str | None,
    message_text: str,
    *,
    candidates: Sequence[TriageCandidate],
) -> TriageDecision:
    """The Directive's outcome: the runtime's answer when it can be trusted, else the
    keyword fallback."""

    try:
        return parse_llm_decision(raw_text, candidates=candidates)
    except TriageDirectiveParseError:
        return fallback_decision(message_text, candidates=candidates)
