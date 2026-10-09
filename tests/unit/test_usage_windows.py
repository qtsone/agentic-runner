"""Subscription usage windows in the heartbeat (local-agents 10).

Four halves: the parsers over each adapter's captured output, the probes against fake
binaries (the Contract's uid, its harness root, no bearer), the per-Contract cadence, and
the ``HarnessVersion`` rows the stream builds from it.
"""

from __future__ import annotations

import asyncio
import json
import resource
import stat
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from agentic_runner import service
from agentic_runner.credentials import CredentialResolver, EmptyCredentialStore
from agentic_runner.harness_self_test import DirectivesInFlight
from agentic_runner.llm_proxy import LlmProxy, SlotStore
from agentic_runner.registration import RunnerState
from agentic_runner.sealed_box import RecipientKeyStore, SealedCredentialStream
from agentic_runner.usage_windows import (
    ClaudeUsageScreenProbe,
    CodexRateLimitsProbe,
    UsageWindows,
    claude_rate_limit_windows,
    claude_usage_screen_windows,
    codex_rate_limit_windows,
)
from agentic_runner.workers.agent_runtime import AgentRuntime
from agentic_runner.workers.contract_isolation import ContractIsolation, DirectiveSandbox
from agentic_runner_contracts import __version__ as contracts_version
from agentic_runner_contracts.runner_registration import (
    CliVersion,
    FloorState,
    HarnessVersion,
    HeartbeatAck,
    HeartbeatEnvelope,
    HostAttestation,
    InstallChannel,
    SessionKind,
    StoreKind,
    UsageWindow,
)

CONTRACT = "11111111-1111-4111-8111-111111111111"
NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
RESET_5H = 1_791_559_200  # 2026-10-09T15:20:00Z
RESET_7D = 1_791_976_800  # 2026-10-14T11:20:00Z

# `account/rateLimits/read` in the shape codex-cli 0.159.2's own schema gives it
# (`codex app-server generate-json-schema`, `v2/GetAccountRateLimitsResponse.json`).
CODEX_RATE_LIMITS = {
    "id": 2,
    "result": {
        "accountId": "acct-redacted",
        "rateLimits": {
            "limitId": "codex",
            "planType": "pro",
            "primary": {"usedPercent": 42, "windowDurationMins": 300, "resetsAt": RESET_5H},
            "secondary": {"usedPercent": 7, "windowDurationMins": 10_080, "resetsAt": RESET_7D},
            "credits": {"hasCredits": False, "unlimited": False, "balance": None},
        },
        "rateLimitsByLimitId": None,
    },
}
# Captured from codex-cli 0.159.2 on a harness root with no login.
CODEX_SIGNED_OUT = {
    "error": {
        "code": -32600,
        "message": "codex account authentication required to read rate limits",
    },
    "id": 2,
}

# claude 2.1.295's `rate_limit_info` schema, as claude-agent-acp 0.88.0 forwards it in a
# `usage_update`'s `_meta["_claude/rateLimit"]`.
CLAUDE_RATE_LIMIT_INFO = {
    "status": "allowed",
    "resetsAt": RESET_5H,
    "rateLimitType": "five_hour",
    "utilization": 0.42,
    "unifiedWindows": {
        "five_hour": {"utilization": 0.42, "resetsAt": RESET_5H},
        "seven_day": {"utilization": 0.071, "resetsAt": RESET_7D},
    },
    "isUsingOverage": False,
}

# The `/usage` panel as Paperclip captured it from the CLI (its quota-windows.test.ts),
# drawn here the way a PTY delivers it: colours, cursor moves and carriage returns.
CLAUDE_USAGE_SCREEN = (
    "\x1b[?25l\x1b[2K> /usage\r\n"
    "\x1b[1m Settings:  Status   Config   \x1b[7mUsage\x1b[0m\r\n\r\n"
    " \x1b[1mCurrent session\x1b[22m\r\n"
    " \x1b[38;5;12m█\x1b[39m 2% used\r\n"
    " Resets 5pm (America/Chicago)\r\n\r\n"
    " \x1b[1mCurrent week (all models)\x1b[22m\r\n"
    " 47% used\r\n"
    " Resets Oct 14 at 7:59am (America/Chicago)\r\n\r\n"
    " Current week (Sonnet only)\r\n"
    " 0% used\r\n"
    " Resets Oct 14 at 8:59am (America/Chicago)\r\n\r\n"
    " Extra usage\r\n"
    " Extra usage not enabled • /extra-usage to enable\r\n"
)

BEARERS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY", "CODEX_API_KEY")


# ------------------------------------------------------------------ the parsers


def test_codex_windows_are_named_by_their_duration() -> None:
    assert codex_rate_limit_windows(CODEX_RATE_LIMITS["result"]) == [
        UsageWindow(
            name="five_hour", used_percent=42, resets_at=datetime.fromtimestamp(RESET_5H, UTC)
        ),
        UsageWindow(
            name="seven_day", used_percent=7, resets_at=datetime.fromtimestamp(RESET_7D, UTC)
        ),
    ]


def test_a_codex_window_of_an_unknown_duration_is_named_by_its_slot() -> None:
    result = {"rateLimits": {"primary": {"usedPercent": 130, "windowDurationMins": 60}}}

    assert codex_rate_limit_windows(result) == [
        UsageWindow(name="primary", used_percent=100, resets_at=None)
    ]


def test_claude_windows_come_from_every_unified_window() -> None:
    assert claude_rate_limit_windows(CLAUDE_RATE_LIMIT_INFO) == [
        UsageWindow(
            name="five_hour", used_percent=42, resets_at=datetime.fromtimestamp(RESET_5H, UTC)
        ),
        UsageWindow(
            name="seven_day", used_percent=7.1, resets_at=datetime.fromtimestamp(RESET_7D, UTC)
        ),
    ]


def test_without_unified_windows_the_limiting_window_is_reported() -> None:
    info = {"status": "rejected", "rateLimitType": "seven_day_opus", "utilization": 1.2}

    assert claude_rate_limit_windows(info) == [
        UsageWindow(name="seven_day_opus", used_percent=100, resets_at=None)
    ]


def test_the_usage_screen_yields_each_window_with_its_reset() -> None:
    windows = claude_usage_screen_windows(CLAUDE_USAGE_SCREEN, NOW)

    assert windows == [
        # 5pm Chicago (CDT, UTC-5) is still ahead on 2026-10-09 12:00Z.
        UsageWindow(
            name="five_hour", used_percent=2, resets_at=datetime(2026, 10, 9, 22, 0, tzinfo=UTC)
        ),
        UsageWindow(
            name="seven_day",
            used_percent=47,
            resets_at=datetime(2026, 10, 14, 12, 59, tzinfo=UTC),
        ),
        UsageWindow(
            name="seven_day_sonnet",
            used_percent=0,
            resets_at=datetime(2026, 10, 14, 13, 59, tzinfo=UTC),
        ),
    ]


def test_the_latest_usage_screen_wins_and_ink_may_drop_the_spaces() -> None:
    redrawn = (
        CLAUDE_USAGE_SCREEN
        + "Current session\r\n25% left\r\nResetsJan2at3pm(UTC)\r\n"
        + "Current week (all models)\r\n50% used\r\n"
    )

    assert claude_usage_screen_windows(redrawn, datetime(2026, 12, 30, tzinfo=UTC)) == [
        UsageWindow(
            name="five_hour", used_percent=75, resets_at=datetime(2027, 1, 2, 15, 0, tzinfo=UTC)
        ),
        UsageWindow(name="seven_day", used_percent=50, resets_at=None),
    ]


@pytest.mark.parametrize(
    ("parse", "output"),
    [
        (codex_rate_limit_windows, CODEX_SIGNED_OUT.get("result")),
        (codex_rate_limit_windows, {"rateLimits": {"primary": {"used": "42%"}}}),
        (codex_rate_limit_windows, ["not", "an", "object"]),
        (claude_rate_limit_windows, {"status": "allowed"}),
        (claude_rate_limit_windows, {"unifiedWindows": {"Five Hour!": {"utilization": 0.1}}}),
        (claude_rate_limit_windows, "five_hour"),
        (lambda text: claude_usage_screen_windows(text, NOW), "Failed to load usage data"),
        (lambda text: claude_usage_screen_windows(text, NOW), "Welcome to Claude Code\r\n> "),
    ],
    ids=[
        "codex_signed_out",
        "codex_unknown_window",
        "codex_not_an_object",
        "claude_no_window",
        "claude_unnamed_window",
        "claude_not_an_object",
        "screen_load_failure",
        "screen_without_usage",
    ],
)
def test_an_unknown_shape_yields_no_windows(parse: Any, output: object) -> None:
    assert parse(output) == []


# ------------------------------------------------------------------ the probes


def _isolation(tmp_path: Path) -> ContractIsolation:
    return ContractIsolation(
        workspace_root=tmp_path / "workspaces",
        state_dir=tmp_path / "state",
        uid_min=61_000,
        uid_max=61_009,
        max_processes=4096,
        # macOS refuses any finite RLIMIT_DATA, so the real spawns below run unbounded.
        memory_limit_bytes=resource.RLIM_INFINITY,
        can_separate_uids=False,
    )


def _fake(bin_dir: Path, name: str, body: str) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / name
    script.write_text(f"#!{sys.executable}\n{body}\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)


def _record(seen: Path) -> str:
    return (
        "import json, os, sys\n"
        f"json.dump({{'env': dict(os.environ), 'argv': sys.argv[1:], 'cwd': os.getcwd()}},"
        f" open({str(seen)!r}, 'w'))\n"
    )


def _fake_codex(bin_dir: Path, seen: Path, answer: dict[str, Any]) -> None:
    _fake(
        bin_dir,
        "codex",
        _record(seen)
        + "for line in sys.stdin:\n"
        + "    message = json.loads(line)\n"
        + "    if message.get('id') == 1:\n"
        + "        print(json.dumps({'id': 1, 'result': {}}), flush=True)\n"
        + "    elif message.get('id') == 2:\n"
        + "        print(json.dumps({'method': 'remoteControl/status/changed'}), flush=True)\n"
        + f"        print(json.dumps({answer!r}), flush=True)\n",
    )


def _fake_claude(bin_dir: Path, seen: Path, screen: str) -> None:
    _fake(
        bin_dir,
        "claude",
        _record(seen)
        + "import time\n"
        + "sys.stdout.write('> '); sys.stdout.flush()\n"
        + f"with open({str(seen) + '.typed'!r}, 'w') as typed: typed.write(sys.stdin.readline())\n"
        + f"sys.stdout.write({screen!r}); sys.stdout.flush()\n"
        + "time.sleep(60)\n",
    )


@pytest.fixture
def runner_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """The Runner's own process holds every bearer a probe must not inherit."""

    for name in BEARERS:
        monkeypatch.setenv(name, "sk-must-not-reach-the-probe")
    bin_dir = tmp_path / "bin"
    monkeypatch.setenv("PATH", f"{bin_dir}:/bin:/usr/bin")
    return bin_dir


@pytest.mark.asyncio
async def test_the_codex_probe_reads_the_contracts_harness_root_without_a_bearer(
    tmp_path: Path, runner_env: Path
) -> None:
    seen = tmp_path / "seen.json"
    _fake_codex(runner_env, seen, CODEX_RATE_LIMITS)
    sandbox = _isolation(tmp_path).sandbox(CONTRACT, runtime_kind="codex_cli")

    windows = await CodexRateLimitsProbe()(sandbox)

    assert [window.name for window in windows] == ["five_hour", "seven_day"]
    recorded = json.loads(seen.read_text())
    assert recorded["argv"] == ["app-server"]
    assert recorded["env"]["CODEX_HOME"] == str(sandbox.harness_config_dir)
    assert recorded["env"]["HOME"] == str(sandbox.home_dir)
    assert not set(BEARERS) & set(recorded["env"])


@pytest.mark.asyncio
async def test_a_signed_out_codex_root_yields_no_windows(tmp_path: Path, runner_env: Path) -> None:
    _fake_codex(runner_env, tmp_path / "seen.json", CODEX_SIGNED_OUT)
    sandbox = _isolation(tmp_path).sandbox(CONTRACT, runtime_kind="codex_cli")

    assert await CodexRateLimitsProbe()(sandbox) == []


@pytest.mark.asyncio
async def test_the_claude_probe_types_usage_into_the_contracts_cli_without_a_bearer(
    tmp_path: Path, runner_env: Path
) -> None:
    seen = tmp_path / "seen.json"
    _fake_claude(runner_env, seen, CLAUDE_USAGE_SCREEN)
    sandbox = _isolation(tmp_path).sandbox(CONTRACT, runtime_kind="claude_code")

    windows = await ClaudeUsageScreenProbe(settle_seconds=0.3, clock=lambda: NOW)(sandbox)

    assert [window.name for window in windows] == ["five_hour", "seven_day", "seven_day_sonnet"]
    assert Path(f"{seen}.typed").read_text().strip() == "/usage"
    recorded = json.loads(seen.read_text())
    assert recorded["env"]["CLAUDE_CONFIG_DIR"] == str(sandbox.harness_config_dir)
    assert recorded["env"]["HOME"] == str(sandbox.home_dir)
    assert recorded["cwd"] == str(sandbox.home_dir.resolve())
    # No `oauth-token` in this root, so the launcher exported none either.
    assert not {*BEARERS, "CLAUDE_CODE_OAUTH_TOKEN"} & set(recorded["env"])


@pytest.mark.asyncio
async def test_a_claude_screen_that_never_draws_is_given_up_on(
    tmp_path: Path, runner_env: Path
) -> None:
    _fake_claude(runner_env, tmp_path / "seen.json", "Welcome to Claude Code\r\n")
    sandbox = _isolation(tmp_path).sandbox(CONTRACT, runtime_kind="claude_code")

    probe = ClaudeUsageScreenProbe(settle_seconds=0.1, timeout_seconds=1.5, clock=lambda: NOW)

    assert await probe(sandbox) == []


@pytest.mark.parametrize("probe", [CodexRateLimitsProbe(), ClaudeUsageScreenProbe()])
@pytest.mark.asyncio
async def test_each_probe_spawns_under_the_contracts_uid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, probe: Any
) -> None:
    contract_uid = 61_004
    spawned: list[dict[str, Any]] = []

    async def spawn(*_argv: str, **kwargs: Any) -> Any:
        spawned.append(kwargs)
        raise PermissionError("recorded")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    sandbox = DirectiveSandbox(
        home_dir=tmp_path,
        harness_config_dir=tmp_path / "harness",
        max_processes=64,
        max_memory_bytes=None,
        uid=contract_uid,
        gid=contract_uid,
    )

    with pytest.raises(PermissionError):
        await probe(sandbox)

    [kwargs] = spawned
    assert (kwargs["user"], kwargs["group"]) == (contract_uid, contract_uid)
    assert kwargs["start_new_session"] is True


# ------------------------------------------------------------------ the cadence


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class _Probe:
    def __init__(self, *windows: UsageWindow, error: Exception | None = None) -> None:
        self.windows = list(windows)
        self.error = error
        self.calls = 0

    async def __call__(self, sandbox: DirectiveSandbox) -> list[UsageWindow]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.windows


FIVE_HOUR = UsageWindow(
    name="five_hour", used_percent=42, resets_at=datetime(2026, 10, 9, 15, tzinfo=UTC)
)


def _usage_windows(
    tmp_path: Path,
    probe: _Probe,
    *,
    cli_kind: str = "codex_cli",
    in_flight: DirectivesInFlight | None = None,
    monotonic: _Clock | None = None,
    clock: Any = lambda: NOW,
) -> UsageWindows:
    isolation = _isolation(tmp_path)
    isolation.sandbox(CONTRACT, runtime_kind=cli_kind)
    return UsageWindows(
        probes={cli_kind: probe},
        sandbox_for=lambda contract_id, kind: isolation.existing_sandbox(
            contract_id, runtime_kind=kind
        ),
        in_flight=in_flight or DirectivesInFlight(),
        monotonic=monotonic or _Clock(),
        clock=clock,
    )


TARGETS: Sequence[tuple[str, str]] = [(CONTRACT, "codex_cli")]


@pytest.mark.asyncio
async def test_a_contract_is_probed_at_most_once_per_fifteen_minutes(tmp_path: Path) -> None:
    probe, clock = _Probe(FIVE_HOUR), _Clock()
    usage = _usage_windows(tmp_path, probe, monotonic=clock)

    await usage.run_due(TARGETS)
    clock.now = 14 * 60
    await usage.run_due(TARGETS)
    assert probe.calls == 1
    assert usage.windows(CONTRACT, "codex_cli") == [FIVE_HOUR]

    clock.now = 15 * 60
    await usage.run_due(TARGETS)
    assert probe.calls == 2


@pytest.mark.asyncio
async def test_never_while_the_contract_has_a_directive_running(tmp_path: Path) -> None:
    probe, in_flight = _Probe(FIVE_HOUR), DirectivesInFlight()
    usage = _usage_windows(tmp_path, probe, in_flight=in_flight)

    with in_flight.running(CONTRACT):
        await usage.run_due(TARGETS)
    assert probe.calls == 0

    await usage.run_due(TARGETS)
    assert probe.calls == 1


@pytest.mark.asyncio
async def test_a_directive_stopped_on_its_usage_limit_makes_the_probe_due(tmp_path: Path) -> None:
    probe, clock = _Probe(FIVE_HOUR), _Clock()
    usage = _usage_windows(tmp_path, probe, monotonic=clock)
    await usage.run_due(TARGETS)
    clock.now = 60

    usage.after_directive(CONTRACT, "codex_cli", (), usage_limit=False)
    await usage.run_due(TARGETS)
    assert probe.calls == 1

    usage.after_directive(CONTRACT, "codex_cli", (), usage_limit=True)
    await usage.run_due(TARGETS)
    assert probe.calls == 2


@pytest.mark.asyncio
async def test_windows_the_directives_stream_carried_are_reported_and_never_probed(
    tmp_path: Path,
) -> None:
    probe, clock = _Probe(), _Clock()
    usage = _usage_windows(tmp_path, probe, cli_kind="claude_code", monotonic=clock)

    usage.after_directive(CONTRACT, "claude_code", (FIVE_HOUR,), usage_limit=True)
    clock.now = 24 * 60 * 60
    await usage.run_due([(CONTRACT, "claude_code")])

    assert probe.calls == 0
    assert usage.windows(CONTRACT, "claude_code") == [FIVE_HOUR]


@pytest.mark.parametrize(
    "error", [TimeoutError(), PermissionError(), ValueError("unparseable")], ids=str
)
@pytest.mark.asyncio
async def test_a_probe_that_fails_reports_nothing(tmp_path: Path, error: Exception) -> None:
    clock = _Clock()
    usage = _usage_windows(tmp_path, _Probe(FIVE_HOUR), monotonic=clock)
    await usage.run_due(TARGETS)
    assert usage.windows(CONTRACT, "codex_cli") == [FIVE_HOUR]

    usage._probes["codex_cli"] = _Probe(error=error)
    clock.now = 15 * 60
    await usage.run_due(TARGETS)

    assert usage.windows(CONTRACT, "codex_cli") is None


@pytest.mark.asyncio
async def test_a_window_past_its_reset_is_dropped(tmp_path: Path) -> None:
    now = [NOW]
    usage = _usage_windows(tmp_path, _Probe(FIVE_HOUR), clock=lambda: now[0])
    await usage.run_due(TARGETS)

    now[0] = datetime(2026, 10, 9, 15, 0, tzinfo=UTC)

    assert usage.windows(CONTRACT, "codex_cli") is None


@pytest.mark.asyncio
async def test_a_root_gone_from_the_sweep_drops_its_windows(tmp_path: Path) -> None:
    usage = _usage_windows(tmp_path, _Probe(FIVE_HOUR))
    await usage.run_due(TARGETS)

    await usage.run_due([])

    assert usage.windows(CONTRACT, "codex_cli") is None


# ------------------------------------------------------------------ the heartbeat


class _Registration:
    server_date = None

    def __init__(self) -> None:
        self.sent: list[HeartbeatEnvelope] = []

    async def heartbeat(self, state: Any, envelope: HeartbeatEnvelope) -> HeartbeatAck:
        self.sent.append(envelope)
        return HeartbeatAck(
            runner_id=uuid4(),
            floor_state=FloorState.OK,
            contracts_floor=contracts_version,
            tag_set_version=1,
            accepts_new_directives=True,
        )


def _stream(
    tmp_path: Path, registration: _Registration, usage_windows: UsageWindows | None
) -> service.ControlPlaneStream:
    runner_id = uuid4()
    isolation = _isolation(tmp_path)
    isolation.sandbox(CONTRACT, runtime_kind="codex_cli")
    return service.ControlPlaneStream(
        client=registration,  # type: ignore[arg-type]
        state=RunnerState(
            runner_id=runner_id,
            identity_id="id",
            private_key_pem="unused",
            temporal_namespace="org-x",
            task_queue=f"runner.{runner_id}",
        ),
        isolation=service.IsolationMode.NONE,
        proxy=LlmProxy(slots=SlotStore()),
        sealed=SealedCredentialStream(RecipientKeyStore(tmp_path / "keys")),
        credentials=CredentialResolver(store=EmptyCredentialStore()),
        load=service._LoadInterceptor(),
        attestation=HostAttestation(
            build_id="a" * 64,
            install_channel=InstallChannel.HOMEBREW,
            os="darwin 27.0",
            session_kind=SessionKind.LOGIN_AGENT,
            store_kind=StoreKind.KEYCHAIN,
            clis=[CliVersion(cli_kind="codex_cli", version="0.159.2", meets_floor=True)],
        ),
        contracts=isolation,
        usage_windows=usage_windows,
    )


@pytest.mark.asyncio
async def test_the_heartbeat_carries_the_windows_after_a_probe(tmp_path: Path) -> None:
    usage = _usage_windows(tmp_path, _Probe(FIVE_HOUR))
    await usage.run_due(TARGETS)
    registration = _Registration()

    await _stream(tmp_path, registration, usage).exchange([])

    [row] = registration.sent[-1].harnesses
    assert (row.contract_id, row.auth_mode, row.usage_windows) == (
        UUID(CONTRACT),
        "login",
        [FIVE_HOUR],
    )


@pytest.mark.asyncio
async def test_an_organisation_hosted_runner_reports_no_windows(tmp_path: Path) -> None:
    runtimes: dict[str, AgentRuntime] = {}
    for party in ("organisation", "account", ""):
        assert (
            service.build_usage_windows(
                host_party=party,
                runtimes=runtimes,
                isolation=_isolation(tmp_path),
                in_flight=DirectivesInFlight(),
            )
            is None
        )
    registration = _Registration()

    await _stream(tmp_path, registration, None).exchange([])

    body = json.loads(registration.sent[-1].model_dump_json())
    [row] = body["harnesses"]
    assert "usage_windows" not in row


def test_a_user_hosted_runner_probes_only_the_harnesses_it_serves(tmp_path: Path) -> None:
    usage = service.build_usage_windows(
        host_party="user",
        runtimes={"codex_cli": object()},  # type: ignore[dict-item]
        isolation=_isolation(tmp_path),
        in_flight=DirectivesInFlight(),
    )

    assert usage is not None
    assert list(usage._probes) == ["codex_cli"]


def test_an_older_control_planes_parse_is_unaffected() -> None:
    older = {
        "contract_id": CONTRACT,
        "cli_kind": "codex_cli",
        "version": "0.159.2",
        "auth_mode": "login",
    }

    row = HarnessVersion.model_validate(older)

    assert row.usage_windows is None
    # Absent stays absent on the wire: the body a control plane on 3.6 already parses.
    assert row.model_dump(mode="json") == older
    with_windows = HarnessVersion(**row.model_dump(), usage_windows=[FIVE_HOUR])
    assert HarnessVersion.model_validate_json(with_windows.model_dump_json()) == with_windows


def test_a_window_carries_a_percentage_and_a_time_only() -> None:
    with pytest.raises(ValueError):
        UsageWindow.model_validate({"name": "five_hour", "used_percent": 10, "email": "x@y"})
    with pytest.raises(ValueError):
        UsageWindow.model_validate({"name": "five_hour", "used_percent": 101})
