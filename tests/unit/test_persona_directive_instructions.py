"""Persona instructions in Directive prompts (persona-specialists issue 02).

These tests pin the three load-bearing properties of the injection: absent instructions
leave both Directive prompts byte-identical to the pre-Persona form (the slice is a no-op
until an operator writes some), the pipeline guardrail follows the instructions and is
named as taking precedence, and an oversized instructions blob is head-truncated with a
note instead of failing the Directive. Evidence attribution (Persona slug + instructions
content hash) rides the same seam.
"""

from __future__ import annotations

import hashlib

from agentic_runner.activities import (
    _ASK_HINT,
    _PERSONA_INSTRUCTIONS_LIMIT_BYTES,
    _codex_evidence_payload,
    _directive_prompt,
    _fix_directive_prompt,
    _persona_instructions_hash,
    _persona_prompt_preamble,
)
from agentic_runner.workers.agent_runtime import DirectiveEvidence, DirectiveResult

_TASK = "Add a Production section to the README"
_PR_BODY = "Task: Add a Production section to the README\n\nWork record: wr-1"
_SUMMARY = "Verifier failed with exit code 1; command abc123"
_INSTRUCTIONS = "You are a senior README curator. Prefer terse prose over bullet walls."


def _preamble(instructions: str, *, slug: str | None = "dev-engineer") -> str:
    notes: list[str] = []
    # PRD issue 56: a Persona with no accepted Lessons carries empty Experience, and every
    # byte-identical fixture below must hold through that path too.
    return _persona_prompt_preamble(
        persona_slug=slug, instructions=instructions, notes=notes, experience=""
    )


def _directive_result() -> DirectiveResult:
    return DirectiveResult(
        exit_code=0,
        stdout="ok",
        stderr="",
        error="",
        command_hash="hash",
        evidence=DirectiveEvidence(
            workspace_id="qtsone/agentic-os/record-1",
            base_branch="main",
            work_branch="agent/record-1",
            command_hash="hash",
            guard_mode="sandbox=danger-full-access (pod-isolated)",
            notes=[],
        ),
    )


def test_empty_instructions_leave_initial_prompt_byte_identical() -> None:
    expected = (
        "Implement the following change in the current repository workspace.\n\n"
        f"Task:\n{_TASK}\n\n"
        "Edit files only. Do not run git commit or git push and do not open a pull "
        "request — the pipeline commits, pushes, and manages the PR. When only a person "
        f"can unblock you, {_ASK_HINT}\n\n"
        f"{_PR_BODY}"
    )
    for instructions in ("", "   \n\t"):
        prompt = _directive_prompt(
            completion_criteria=_TASK,
            pr_body=_PR_BODY,
            persona_preamble=_preamble(instructions),
        )
        assert prompt == expected


def test_empty_instructions_leave_fix_prompt_byte_identical() -> None:
    expected = (
        "The verifier failed for the change on this branch. Read the failure summary "
        "below, fix the code so the verifier passes, and keep the change minimal.\n\n"
        f"Verifier failure:\n{_SUMMARY}\n\nOriginal task:\n{_TASK}"
    )
    prompt = _fix_directive_prompt(_SUMMARY, _TASK, persona_preamble=_preamble(""))
    assert prompt == expected


def test_initial_prompt_prepends_instructions_and_guardrail_follows_them() -> None:
    prompt = _directive_prompt(
        completion_criteria=_TASK,
        pr_body=_PR_BODY,
        persona_preamble=_preamble(_INSTRUCTIONS),
    )

    assert prompt.startswith("Persona instructions (dev-engineer):\n")
    assert _INSTRUCTIONS in prompt
    precedence = "The pipeline rules take precedence over the Persona instructions above"
    pipeline_rule = "Do not run git commit or git push and do not open a pull request"
    assert prompt.index(_INSTRUCTIONS) < prompt.index(precedence)
    assert precedence in prompt
    assert pipeline_rule in prompt
    assert f"Task:\n{_TASK}" in prompt


def test_fix_prompt_prepends_instructions_and_restates_the_guardrail() -> None:
    prompt = _fix_directive_prompt(_SUMMARY, _TASK, persona_preamble=_preamble(_INSTRUCTIONS))

    assert prompt.startswith("Persona instructions (dev-engineer):\n")
    # The fix base prompt has no guardrail of its own, so the preamble's restated
    # pipeline rule is the only thing keeping operator text from claiming commit/push.
    assert "do not run git commit or git push" in prompt
    assert prompt.index(_INSTRUCTIONS) < prompt.index("The verifier failed")
    assert f"Verifier failure:\n{_SUMMARY}" in prompt


def test_oversized_instructions_are_head_truncated_with_a_note() -> None:
    lead = "Lead with this domain rule."
    blob = lead + ("x" * (_PERSONA_INSTRUCTIONS_LIMIT_BYTES * 2))
    notes: list[str] = []

    preamble = _persona_prompt_preamble(persona_slug="dev-engineer", instructions=blob, notes=notes)

    assert lead in preamble
    assert notes == [f"persona_instructions truncated to {_PERSONA_INSTRUCTIONS_LIMIT_BYTES} bytes"]
    # Preamble stays near the cap: bounded instructions plus the fixed framing text.
    assert len(preamble.encode()) <= _PERSONA_INSTRUCTIONS_LIMIT_BYTES + 400


def test_instructions_hash_names_the_content_version() -> None:
    assert _persona_instructions_hash("") is None
    assert _persona_instructions_hash("  \n") is None
    assert (
        _persona_instructions_hash(_INSTRUCTIONS)
        == hashlib.sha256(_INSTRUCTIONS.encode()).hexdigest()
    )


def test_codex_evidence_records_persona_attribution_and_prompt_truncation() -> None:
    prompt_notes = [f"persona_instructions truncated to {_PERSONA_INSTRUCTIONS_LIMIT_BYTES} bytes"]

    payload = _codex_evidence_payload(
        _directive_result(),
        persona_slug="dev-engineer",
        persona_instructions=_INSTRUCTIONS,
        prompt_notes=prompt_notes,
    )

    assert payload["persona_slug"] == "dev-engineer"
    assert payload["persona_instructions_hash"] == (
        hashlib.sha256(_INSTRUCTIONS.encode()).hexdigest()
    )
    assert payload["truncation_notes"] == prompt_notes


def test_codex_evidence_without_persona_records_null_attribution() -> None:
    payload = _codex_evidence_payload(_directive_result())

    assert payload["persona_slug"] is None
    assert payload["persona_instructions_hash"] is None
    assert "truncation_notes" not in payload
