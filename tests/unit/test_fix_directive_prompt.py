"""Fix Directive prompt and verifier failure-output plumbing.

The fix Directive prompt used to carry only the terse one-line verifier summary
("Verifier failed with exit code 1; command <hash>"), so the fix runtime had to
re-run the entire suite in the workspace just to rediscover which tests failed
(observed on work record f6223d2a-f2ee-43e2-b023-aefecda953f9). These tests pin
the bounded, redacted output tail that now travels with the summary.
"""

from __future__ import annotations

from agentic_runner.activities import (
    _VERIFIER_FAILURE_OUTPUT_LIMIT_BYTES,
    _fix_directive_prompt,
    _verifier_failure_output,
)
from agentic_runner.runtime.verifier_command import VerificationResult
from agentic_runner.workers._runtime_support import bound_text_tail

_SUMMARY = "Verifier failed with exit code 1; command abc123"
_TASK = "Add a Production section to the README"


def _failed_result(*, stdout: str = "", stderr: str = "") -> VerificationResult:
    return VerificationResult(
        command_hash="abc123",
        exit_code=1,
        passed=False,
        stdout=stdout,
        stderr=stderr,
        elapsed_ms=1234,
        working_directory="/workspace",
        argv=("python", "-m", "pytest", "-q"),
        env_keys=("PATH",),
    )


def test_prompt_places_verifier_output_between_summary_and_task() -> None:
    output = "--- verifier stdout (tail) ---\nFAILED tests/test_x.py::test_y - AssertionError"

    prompt = _fix_directive_prompt(_SUMMARY, _TASK, verifier_output=output)

    assert _SUMMARY in prompt
    assert f"Verifier output:\n{output}" in prompt
    assert f"Original task:\n{_TASK}" in prompt
    assert prompt.index(_SUMMARY) < prompt.index("Verifier output:") < prompt.index(_TASK)


def test_prompt_omits_output_section_when_no_output_was_captured() -> None:
    for empty in ("", "   \n"):
        prompt = _fix_directive_prompt(_SUMMARY, _TASK, verifier_output=empty)
        assert "Verifier output:" not in prompt
        assert _SUMMARY in prompt
        assert f"Original task:\n{_TASK}" in prompt


def test_failure_output_carries_labeled_tails_of_both_streams() -> None:
    output = _verifier_failure_output(
        _failed_result(
            stdout="FAILED tests/test_x.py::test_y - AssertionError\n1 failed in 2.31s",
            stderr="warning: config deprecated",
        )
    )

    assert "--- verifier stdout (tail) ---" in output
    assert "FAILED tests/test_x.py::test_y - AssertionError" in output
    assert "--- verifier stderr (tail) ---" in output
    assert "warning: config deprecated" in output


def test_failure_output_skips_empty_streams() -> None:
    output = _verifier_failure_output(_failed_result(stdout="1 failed in 2.31s"))

    assert "stdout" in output
    assert "stderr" not in output
    assert _verifier_failure_output(_failed_result()) == ""


def test_failure_output_keeps_the_tail_and_notes_the_truncation() -> None:
    filler = "collected 500 items\n" * 2_000
    tail_marker = "FAILED tests/test_last.py::test_tail - AssertionError"
    output = _verifier_failure_output(_failed_result(stdout=filler + tail_marker))

    assert tail_marker in output
    assert f"[stdout truncated to last {_VERIFIER_FAILURE_OUTPUT_LIMIT_BYTES} bytes]" in output
    # Bounded far below Temporal's payload ceiling: one stream tail plus a short header.
    assert len(output.encode()) <= _VERIFIER_FAILURE_OUTPUT_LIMIT_BYTES + 200


def test_failure_output_redacts_secret_like_text() -> None:
    token = "ghp_1234567890abcdef1234567890abcdef1234"
    output = _verifier_failure_output(_failed_result(stdout=f"cloning https://x@y token {token}"))

    assert token not in output
    assert "[REDACTED]" in output


def test_bound_text_tail_returns_short_text_unchanged_without_note() -> None:
    notes: list[str] = []

    assert bound_text_tail("short", 64, notes, "stdout") == "short"
    assert notes == []


def test_bound_text_tail_keeps_last_bytes_and_appends_note() -> None:
    notes: list[str] = []
    text = "head-" + "x" * 64 + "-tail"

    bounded = bound_text_tail(text, 16, notes, "stdout")

    assert bounded.endswith("-tail")
    assert "head-" not in bounded
    assert len(bounded.encode()) == 16
    assert notes == ["stdout truncated to last 16 bytes"]
