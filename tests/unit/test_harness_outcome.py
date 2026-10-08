"""The subscription-harness classifier and the ``harness_hold`` contract (local-agents 08).

Every sample names where it comes from. ``spike`` is a capture from the local-agents 02
ACP spike (2026-09-29, claude 2.1.284 / claude-agent-acp 0.84.0 and codex 0.135 /
codex-acp 2.0.0, against a fake model endpoint), rendered the way the ACP runtime raises
it. ``paperclip`` is a message from Paperclip's ``claude-local`` / ``codex-local`` tests at
``6bc830b`` -- no real Claude usage limit has been captured yet.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from temporalio.converter import DataConverter

from agentic_runner.workers.harness_outcome import classify_harness_outcome
from agentic_runner_contracts.activity_io import (
    BranchPullRequestOutput,
    FixDirectiveOutput,
    HarnessHold,
    HarnessHoldKind,
    LearningDirectiveOutput,
    MemberDirectiveOutput,
)

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
USAGE = HarnessHoldKind.USAGE_LIMIT
SIGN_IN = HarnessHoldKind.SIGN_IN_REQUIRED


def _acp(code: int, message: str, data: object = None) -> str:
    detail = "" if data is None else f" {json.dumps(data, sort_keys=True, default=str)}"
    return f"ACP error {code}: {message}{detail}"


def _codex_error(message: str) -> str:
    return "\n".join(
        [
            json.dumps({"type": "thread.started", "thread_id": "t-1"}),
            json.dumps({"type": "error", "message": message}),
            json.dumps({"type": "turn.failed", "error": {"message": message}}),
        ]
    )


def _codex_said(text: str) -> str:
    return json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": text}})


def _claude_result(text: str, *, is_error: bool = True, **extra: object) -> str:
    return json.dumps(
        {"type": "result", "subtype": "success", "is_error": is_error, "result": text, **extra}
    )


CLASSIFIED = [
    # --- spike captures, over ACP ---------------------------------------------------------
    pytest.param(
        "codex_cli",
        {
            "error": _acp(
                -32603,
                "Internal error",
                {
                    "message": "You’ve hit your usage limit. Try again later.",
                    "codexErrorInfo": "usageLimitExceeded",
                },
            )
        },
        HarnessHold(USAGE),
        id="spike-xLIM-codex-usage-limit",
    ),
    pytest.param(
        "codex_cli",
        {"error": _acp(-32000, "Authentication required")},
        HarnessHold(SIGN_IN),
        id="spike-xEXP-codex-expired",
    ),
    pytest.param(
        "claude_code",
        {
            "stdout": "Failed to authenticate. API Error: 401 OAuth token has expired",
            "error": _acp(-32000, "Authentication required"),
        },
        HarnessHold(SIGN_IN),
        id="spike-cEXP-claude-expired",
    ),
    pytest.param(
        "a_future_acp_harness",
        {"error": _acp(-32000, "Authentication required")},
        HarnessHold(SIGN_IN),
        id="acp-auth-required-is-protocol-level",
    ),
    # --- Codex, `codex exec --json` ---------------------------------------------------------
    pytest.param(
        "codex_cli",
        {
            "stdout": _codex_error(
                "You've hit your usage limit for GPT-5.3-Codex-Spark. Switch to another model "
                "now, or try again at 11:31 PM (America/Chicago)."
            )
        },
        HarnessHold(USAGE, "2026-10-10T04:31:00+00:00"),
        id="paperclip-codex-limit-reset-in-named-zone",
    ),
    pytest.param(
        "codex_cli",
        {
            "stdout": _codex_error(
                "You've hit your usage limit. Visit https://example.invalid/usage for account "
                "details."
            )
        },
        HarnessHold(USAGE),
        id="paperclip-codex-limit-no-reset",
    ),
    pytest.param(
        "codex_cli",
        {"stdout": _codex_error("You’ve hit your usage limit")},
        HarnessHold(USAGE),
        id="paperclip-codex-limit-curly-apostrophe",
    ),
    pytest.param(
        "codex_cli",
        {"stdout": _codex_error("Usage limit exceeded")},
        HarnessHold(USAGE),
        id="paperclip-codex-limit-exceeded",
    ),
    pytest.param(
        "codex_cli",
        {"stderr": "provider error: refresh_token_reused"},
        HarnessHold(SIGN_IN),
        id="paperclip-codex-refresh-reused",
    ),
    pytest.param(
        "codex_cli",
        {"stderr": "provider error: refresh_token_expired"},
        HarnessHold(SIGN_IN),
        id="paperclip-codex-refresh-expired",
    ),
    pytest.param(
        "codex_cli",
        {"stdout": _codex_error("OAuth failed: refresh_token_invalidated")},
        HarnessHold(SIGN_IN),
        id="paperclip-codex-refresh-invalidated",
    ),
    # --- Claude Code, `claude -p --output-format json` -------------------------------------
    pytest.param(
        "claude_code",
        {"stdout": _claude_result("You've hit your limit · resets 2:30am (UTC)")},
        HarnessHold(USAGE, "2026-10-10T02:30:00+00:00"),
        id="paperclip-claude-limit-reset-utc",
    ),
    pytest.param(
        "claude_code",
        {"stdout": _claude_result("You're out of extra usage · resets 4pm (America/Chicago)")},
        HarnessHold(USAGE, "2026-10-09T21:00:00+00:00"),
        id="paperclip-claude-extra-usage-reset-in-named-zone",
    ),
    pytest.param(
        "claude_code",
        {"stdout": _claude_result("Usage limit reached. Resets at 3:15 AM (UTC).")},
        HarnessHold(USAGE, "2026-10-10T03:15:00+00:00"),
        id="paperclip-claude-usage-limit-reached",
    ),
    pytest.param(
        "claude_code",
        {"stdout": _claude_result("You've hit your weekly limit")},
        HarnessHold(USAGE),
        id="paperclip-claude-weekly-limit-no-reset",
    ),
    pytest.param(
        "claude_code",
        {"stderr": "Invalid API key · Please run /login"},
        HarnessHold(SIGN_IN),
        id="paperclip-claude-please-run-login",
    ),
    pytest.param(
        "claude_code",
        {
            "stdout": _claude_result(
                "Failed to authenticate. API Error: 401 Invalid bearer token",
                api_error_status=401,
                error="authentication_failed",
            )
        },
        HarnessHold(SIGN_IN),
        id="paperclip-claude-invalid-bearer",
    ),
]

NOT_CLASSIFIED = [
    pytest.param(
        "claude_code",
        {
            "error": _acp(
                -32603,
                "Internal error: API Error: Request rejected (429) · rate limited",
                {"errorKind": "rate_limit"},
            )
        },
        id="spike-cF-claude-rate-limit-is-transient",
    ),
    pytest.param(
        "codex_cli",
        {"error": _acp(-32603, "Internal error", {"message": "stream disconnected"})},
        id="acp-internal-error-without-usage-limit-info",
    ),
    pytest.param(
        "codex_cli",
        {
            "stdout": "\n".join(
                [
                    _codex_said("You've hit your usage limit, so try again at 4 PM."),
                    _codex_error("stream disconnected before completion"),
                ]
            )
        },
        id="codex-agent-message-is-the-model-talking",
    ),
    pytest.param(
        "codex_cli",
        {"stdout": _codex_error("We're currently experiencing high demand (429)")},
        id="codex-high-demand-is-transient",
    ),
    pytest.param(
        "codex_cli",
        {"stdout": _codex_error("The model is at capacity for this model")},
        id="codex-capacity-is-not-a-usage-limit",
    ),
    pytest.param(
        "claude_code",
        {
            "stdout": _claude_result(
                "Sure. An invalid bearer token means authentication_failed.", is_error=False
            )
        },
        id="claude-successful-result-quoting-auth-words",
    ),
    pytest.param(
        "claude_code",
        {
            "stdout": "\n".join(
                [
                    json.dumps(
                        {
                            "type": "assistant",
                            "message": {
                                "content": [{"type": "text", "text": "You've hit your limit"}]
                            },
                        }
                    ),
                    _claude_result("API Error: 500 internal server error"),
                ]
            )
        },
        id="claude-assistant-message-is-the-model-talking",
    ),
    pytest.param(
        "claude_code",
        {"stdout": _claude_result("API Error: Request rejected (429) · rate limited")},
        id="claude-rate-limit-is-transient",
    ),
    pytest.param(
        "some_other_cli",
        {"stdout": _codex_error("You've hit your usage limit")},
        id="unknown-harness-text-is-not-read",
    ),
]


@pytest.mark.parametrize(("cli_kind", "output", "expected"), CLASSIFIED)
def test_a_failed_turn_is_classified(
    cli_kind: str, output: dict[str, str], expected: HarnessHold
) -> None:
    assert classify_harness_outcome(cli_kind, exit_code=1, **_streams(output), now=NOW) == expected


@pytest.mark.parametrize(("cli_kind", "output"), NOT_CLASSIFIED)
def test_a_near_miss_is_not_classified(cli_kind: str, output: dict[str, str]) -> None:
    assert classify_harness_outcome(cli_kind, exit_code=1, **_streams(output), now=NOW) is None


@pytest.mark.parametrize(("cli_kind", "output", "expected"), CLASSIFIED)
def test_a_successful_turn_is_never_classified(
    cli_kind: str, output: dict[str, str], expected: HarnessHold
) -> None:
    assert classify_harness_outcome(cli_kind, exit_code=0, **_streams(output), now=NOW) is None


@pytest.fixture
def berlin_host(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("TZ", "Europe/Berlin")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


@pytest.mark.usefixtures("berlin_host")
def test_a_reset_with_no_zone_is_read_on_the_runner_hosts_clock() -> None:
    hold = classify_harness_outcome(
        "codex_cli",
        exit_code=1,
        stdout=_codex_error(
            "You've hit your usage limit for GPT-5. Switch to another model now, or try "
            "again at 11:31 PM."
        ),
        stderr="",
        now=NOW,
    )

    # 23:31 CEST, the same day: 12:00 UTC is 14:00 in Berlin.
    assert hold == HarnessHold(USAGE, "2026-10-09T21:31:00+00:00")


def test_a_reset_already_past_today_is_tomorrows() -> None:
    hold = classify_harness_outcome(
        "claude_code",
        exit_code=1,
        stdout=_claude_result("You've hit your limit · resets 9am (UTC)"),
        stderr="",
        now=NOW,
    )

    assert hold == HarnessHold(USAGE, "2026-10-10T09:00:00+00:00")


def _streams(output: dict[str, str]) -> dict[str, str]:
    return {"stdout": "", "stderr": "", "error": "", **output}


OUTPUTS = [
    BranchPullRequestOutput("o/r", "b", "main", 0, "", False, False),
    FixDirectiveOutput("w", "o/r", 1, 2, "", ""),
    MemberDirectiveOutput("w", "o/r", 1, 2, "", ""),
    LearningDirectiveOutput("w", 2),
]


@pytest.mark.parametrize("output", OUTPUTS, ids=lambda output: type(output).__name__)
def test_harness_hold_round_trips_through_temporal(output: object) -> None:
    converter = DataConverter.default.payload_converter
    held = replace(  # type: ignore[type-var]
        output, harness_hold=HarnessHold(USAGE, "2026-10-10T04:31:00+00:00")
    )

    [decoded] = converter.from_payloads(converter.to_payloads([held]), [type(output)])

    assert decoded == held
    assert decoded.harness_hold.kind is USAGE


@pytest.mark.parametrize("output", OUTPUTS, ids=lambda output: type(output).__name__)
def test_an_output_from_before_harness_hold_still_parses(output: object) -> None:
    converter = DataConverter.default.payload_converter
    [payload] = converter.to_payloads([output])
    older = json.loads(payload.data)
    del older["harness_hold"]
    payload.data = json.dumps(older).encode()

    [decoded] = converter.from_payloads([payload], [type(output)])

    assert decoded == output
    assert decoded.harness_hold is None
