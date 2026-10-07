"""The keyword classifier (PRD issue 29): the Triage Directive's fallback.

The Triage Persona this module once implemented is retired; `classify_specialisation`
survives as the deterministic signal match the Directive falls back to on a hanging or
malformed runtime answer (see ``test_triage_directive.py`` for the Directive's own
own/ask/ignore decision, which wraps this).
"""

from __future__ import annotations

from agentic_runner_contracts.triage import classify_specialisation


def test_clear_development_signal_classifies_as_development() -> None:
    assert classify_specialisation("Please implement the login feature and fix a bug") == (
        "development"
    )


def test_review_and_ops_signals_dominate_generic_vocabulary() -> None:
    assert classify_specialisation("Please review pull request #42 on the auth change") == (
        "review"
    )
    assert classify_specialisation("Restart the worker fleet and roll back the deploy") == "ops"


def test_no_recognizable_signal_is_ambiguous() -> None:
    assert (
        classify_specialisation("hey, can you take a look at this thing when you get a sec?")
        is None
    )
    assert classify_specialisation("good morning team") is None


def test_conflicting_specialised_signals_are_ambiguous() -> None:
    # Review *and* ops both present — never guessed between them.
    assert classify_specialisation("review the PR then deploy and restart it") is None
