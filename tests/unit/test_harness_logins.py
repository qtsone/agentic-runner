"""The subscription state the heartbeat reports per Agent Runtime (QTS-1736, rev 3).

Read from the Contract's harness root by ``stat`` and from the harness's own usage
windows: ``none``, ``signed_in``, ``expired`` (a Claude long-lived token past its year)
and ``at_limit`` until the latest used-up window resets.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agentic_runner.workers.contract_isolation import ContractIsolation
from agentic_runner.workers.harness_logins import subscription_report
from agentic_runner_contracts.runner_registration import SubscriptionState, UsageWindow

CONTRACT = "11111111-1111-4111-8111-111111111111"
NOW = datetime(2026, 10, 16, 12, tzinfo=UTC)


@pytest.fixture
def isolation(tmp_path: Path) -> ContractIsolation:
    return ContractIsolation(
        workspace_root=tmp_path,
        state_dir=tmp_path / ".state",
        uid_min=20_000,
        uid_max=20_100,
        max_processes=64,
        memory_limit_bytes=1 << 30,
        can_separate_uids=False,
    )


def _login(isolation: ContractIsolation, cli_kind: str, name: str, *, age: timedelta) -> None:
    path = isolation.harness_config_dir(CONTRACT, cli_kind) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("never-read")
    written = (NOW - age).timestamp()
    os.utime(path, (written, written))


def _state(
    isolation: ContractIsolation, cli_kind: str, windows: list[UsageWindow] | None = None
) -> tuple[SubscriptionState, datetime | None]:
    report = subscription_report(isolation, CONTRACT, cli_kind, windows=windows, now=NOW)
    return report.state, report.resets_at


def test_no_login_is_none(isolation: ContractIsolation) -> None:
    assert _state(isolation, "codex_cli") == (SubscriptionState.NONE, None)


def test_a_login_is_signed_in(isolation: ContractIsolation) -> None:
    _login(isolation, "codex_cli", "auth.json", age=timedelta(days=400))
    assert _state(isolation, "codex_cli") == (SubscriptionState.SIGNED_IN, None)


def test_a_claude_token_past_its_year_is_expired(isolation: ContractIsolation) -> None:
    _login(isolation, "claude_code", "oauth-token", age=timedelta(days=366))
    assert _state(isolation, "claude_code") == (SubscriptionState.EXPIRED, None)


def test_a_used_up_window_holds_until_the_latest_reset(isolation: ContractIsolation) -> None:
    _login(isolation, "claude_code", "oauth-token", age=timedelta(days=1))
    windows = [
        UsageWindow(name="five_hour", used_percent=100.0, resets_at=NOW + timedelta(hours=2)),
        UsageWindow(name="seven_day", used_percent=100.0, resets_at=NOW + timedelta(days=3)),
        UsageWindow(name="seven_day_opus", used_percent=40.0, resets_at=NOW + timedelta(days=9)),
    ]

    assert _state(isolation, "claude_code", windows) == (
        SubscriptionState.AT_LIMIT,
        NOW + timedelta(days=3),
    )


def test_a_window_whose_reset_has_passed_holds_nothing(isolation: ContractIsolation) -> None:
    _login(isolation, "codex_cli", "auth.json", age=timedelta(days=1))
    windows = [
        UsageWindow(name="five_hour", used_percent=100.0, resets_at=NOW - timedelta(minutes=1))
    ]

    assert _state(isolation, "codex_cli", windows) == (SubscriptionState.SIGNED_IN, None)
