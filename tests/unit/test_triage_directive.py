"""The Triage Directive's decision: structured output, or the keyword fallback (PRD issue 29).

`.scratch/multi-org-release-1/issues/29-triage-directive-intake-brief-work-accept.md`.
"""

from __future__ import annotations

import json
from uuid import uuid4

from agentic_runner_contracts.triage import (
    TriageCandidate,
    decide,
    fallback_decision,
    parse_llm_decision,
)

_DEV_AGENT = uuid4()
_REVIEW_AGENT = uuid4()
_CANDIDATES = [
    TriageCandidate(agent_id=_DEV_AGENT, specialisation="development"),
    TriageCandidate(agent_id=_REVIEW_AGENT, specialisation="review"),
]


def _own(agent_id: str, reason: str = "clear request") -> str:
    return json.dumps({"outcome": "own", "lead_agent_id": agent_id, "reason": reason})


def test_own_with_a_valid_candidate_lead_parses_from_the_runtime_answer() -> None:
    decision = parse_llm_decision(_own(str(_DEV_AGENT)), candidates=_CANDIDATES)

    assert decision.outcome == "own"
    assert decision.lead_agent_id == _DEV_AGENT
    assert decision.source == "llm"


def test_ask_and_ignore_need_no_lead_agent_id() -> None:
    ask = parse_llm_decision(
        json.dumps({"outcome": "ask", "lead_agent_id": None, "reason": "not sure"}),
        candidates=_CANDIDATES,
    )
    assert ask.outcome == "ask"
    assert ask.lead_agent_id is None

    ignore = parse_llm_decision(
        json.dumps({"outcome": "ignore", "reason": "just chatter"}),
        candidates=_CANDIDATES,
    )
    assert ignore.outcome == "ignore"
    assert ignore.lead_agent_id is None


def test_no_runtime_output_fails_to_parse() -> None:
    try:
        parse_llm_decision(None, candidates=_CANDIDATES)
    except Exception as error:  # noqa: BLE001 — asserting on the fallback trigger, not the type
        assert "no runtime output" in str(error)
    else:
        raise AssertionError("expected a parse failure")


def test_malformed_json_fails_to_parse() -> None:
    try:
        parse_llm_decision("not json at all", candidates=_CANDIDATES)
    except Exception as error:  # noqa: BLE001
        assert "malformed JSON" in str(error)
    else:
        raise AssertionError("expected a parse failure")


def test_own_with_a_hallucinated_lead_agent_id_fails_to_parse() -> None:
    try:
        parse_llm_decision(_own(str(uuid4())), candidates=_CANDIDATES)
    except Exception as error:  # noqa: BLE001
        assert "not a candidate Lead" in str(error)
    else:
        raise AssertionError("expected a parse failure -- never guess a Lead")


def test_fallback_matches_a_candidate_by_specialisation() -> None:
    decision = fallback_decision(
        "please implement the login feature and fix the bug", candidates=_CANDIDATES
    )
    assert decision.outcome == "own"
    assert decision.lead_agent_id == _DEV_AGENT
    assert decision.source == "fallback"


def test_fallback_never_guesses_ignore_or_own_without_a_candidate() -> None:
    # Unclassifiable text: defers to a human, never guessed (the never-guess invariant).
    ambiguous = fallback_decision("hey, can you take a look at this thing?", candidates=_CANDIDATES)
    assert ambiguous.outcome == "ask"
    assert ambiguous.source == "fallback"

    # Classifiable, but no matching candidate is deployed: still ask, never own.
    no_match = fallback_decision("please deploy and restart the ops fleet", candidates=_CANDIDATES)
    assert no_match.outcome == "ask"


def test_decide_falls_back_on_a_hang_or_a_malformed_answer() -> None:
    hang = decide(None, "please implement the login feature", candidates=_CANDIDATES)
    assert hang.outcome == "own"
    assert hang.source == "fallback"

    malformed = decide("{not json", "please implement the login feature", candidates=_CANDIDATES)
    assert malformed.outcome == "own"
    assert malformed.source == "fallback"


def test_decide_trusts_a_well_formed_runtime_answer() -> None:
    decision = decide(
        _own(str(_REVIEW_AGENT)), "please review pull request #7", candidates=_CANDIDATES
    )
    assert decision.outcome == "own"
    assert decision.lead_agent_id == _REVIEW_AGENT
    assert decision.source == "llm"
