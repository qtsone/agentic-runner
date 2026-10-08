"""The harness capability descriptor on the heartbeat (local-agents 17).

Three halves: the ``--version`` self-test run as each Contract would run it, the
optional ``HarnessVersion`` fields that carry it, and the heartbeat rows the stream
builds from the runtimes, the harness roots and the self-tests.
"""

from __future__ import annotations

import resource
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from agentic_runner import service
from agentic_runner.credentials import CredentialResolver, EmptyCredentialStore
from agentic_runner.harness_self_test import DirectivesInFlight, HarnessSelfTests
from agentic_runner.llm_proxy import LlmProxy, SlotStore
from agentic_runner.registration import RunnerState
from agentic_runner.sealed_box import RecipientKeyStore, SealedCredentialStream
from agentic_runner.workers._runtime_support import SubprocessResult
from agentic_runner.workers.agent_runtime import (
    AgentRuntime,
    AuthMode,
    DirectiveRequest,
    DirectiveResult,
    RuntimeCapabilities,
)
from agentic_runner.workers.contract_isolation import ContractIsolation, DirectiveSandbox
from agentic_runner_contracts import __version__ as contracts_version
from agentic_runner_contracts.runner_registration import (
    CliVersion,
    FloorState,
    HarnessSelfTest,
    HarnessVersion,
    HeartbeatAck,
    HeartbeatEnvelope,
    HostAttestation,
    InstallChannel,
    SelfTestFailure,
    SessionKind,
    StoreKind,
)

CONTRACT = "11111111-1111-4111-8111-111111111111"
NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


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


def _with_roots(isolation: ContractIsolation, *contract_ids: str) -> ContractIsolation:
    """The harness roots a Contract's first Directive leaves, which the sweep then lists."""

    for contract_id in contract_ids or (CONTRACT,):
        isolation.sandbox(contract_id, runtime_kind="codex_cli")
    return isolation


def _fake_cli(bin_dir: Path, name: str, body: str) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / name
    script.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _self_tests(
    isolation: ContractIsolation,
    *,
    in_flight: DirectivesInFlight | None = None,
    served: tuple[str, ...] = ("codex_cli",),
    monotonic: _Clock | None = None,
    **overrides: Any,
) -> HarnessSelfTests:
    return HarnessSelfTests(
        sandbox_for=lambda contract_id, cli_kind: isolation.existing_sandbox(
            contract_id, runtime_kind=cli_kind
        ),
        in_flight=in_flight or DirectivesInFlight(),
        served=served,
        monotonic=monotonic or _Clock(),
        clock=lambda: NOW,
        **overrides,
    )


# ------------------------------------------------------------------ the self-test


@pytest.mark.asyncio
async def test_the_self_test_runs_the_cli_in_the_contracts_harness_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    isolation = _with_roots(_isolation(tmp_path))
    seen = tmp_path / "seen"
    _fake_cli(
        tmp_path / "bin",
        "codex",
        f'echo "$CODEX_HOME|$HOME|$TMPDIR|$PWD" > {seen}\necho "codex-cli 0.141.0"',
    )
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:/bin:/usr/bin")
    self_tests = _self_tests(isolation)

    await self_tests.run_due([(CONTRACT, "codex_cli")])

    sandbox = isolation.sandbox(CONTRACT, runtime_kind="codex_cli")
    assert seen.read_text().strip().split("|") == [
        str(sandbox.harness_config_dir),
        str(sandbox.home_dir),
        str(sandbox.tmp_dir),
        str(sandbox.home_dir),
    ]
    assert self_tests.result(UUID(CONTRACT), "codex_cli") == HarnessSelfTest(ok=True, at=NOW)


@pytest.mark.asyncio
async def test_the_self_test_spawns_under_the_contracts_uid(tmp_path: Path) -> None:
    contract_uid = 61_004
    spawned: list[DirectiveSandbox | None] = []

    async def run(**kwargs: Any) -> SubprocessResult:
        spawned.append(kwargs["sandbox"])
        return SubprocessResult(exit_code=0, stdout="0.141.0", stderr="")

    sandbox = DirectiveSandbox(
        home_dir=tmp_path,
        harness_config_dir=tmp_path / "harness",
        max_processes=64,
        max_memory_bytes=1024**3,
        uid=contract_uid,
        gid=contract_uid,
    )
    self_tests = HarnessSelfTests(
        sandbox_for=lambda _contract, _kind: sandbox,
        in_flight=DirectivesInFlight(),
        served=("codex_cli",),
        run=run,
        monotonic=_Clock(),
    )

    await self_tests.run_due([(CONTRACT, "codex_cli")])

    [used] = spawned
    assert used is not None
    assert used.spawn_kwargs()["user"] == contract_uid


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ('echo "codex-cli 0.1.0"', SelfTestFailure.BELOW_FLOOR),
        ("exit 3", SelfTestFailure.SPAWN_FAILED),
        ('echo "no version here"', SelfTestFailure.SPAWN_FAILED),
    ],
    ids=["below_floor", "nonzero_exit", "no_version"],
)
@pytest.mark.asyncio
async def test_a_failed_self_test_reports_its_reason_code_and_never_the_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str, reason: SelfTestFailure
) -> None:
    _fake_cli(tmp_path / "bin", "codex", body)
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:/bin:/usr/bin")
    self_tests = _self_tests(_with_roots(_isolation(tmp_path)))

    await self_tests.run_due([(CONTRACT, "codex_cli")])

    assert self_tests.result(CONTRACT, "codex_cli") == HarnessSelfTest(
        ok=False, at=NOW, reason=reason
    )


@pytest.mark.asyncio
async def test_a_cli_missing_from_path_is_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))
    self_tests = _self_tests(_with_roots(_isolation(tmp_path)))

    await self_tests.run_due([(CONTRACT, "codex_cli")])

    result = self_tests.result(CONTRACT, "codex_cli")
    assert result is not None and result.reason == SelfTestFailure.NOT_FOUND


@pytest.mark.asyncio
async def test_a_cli_kind_the_runner_does_not_serve_is_not_found(tmp_path: Path) -> None:
    self_tests = _self_tests(_isolation(tmp_path), served=("codex_cli",))

    await self_tests.run_due([(CONTRACT, "claude_code")])

    result = self_tests.result(CONTRACT, "claude_code")
    assert result is not None and result.reason == SelfTestFailure.NOT_FOUND


@pytest.mark.parametrize(
    ("raised", "reason"),
    [(TimeoutError(), SelfTestFailure.TIMEOUT), (PermissionError(), SelfTestFailure.SPAWN_FAILED)],
    ids=["timeout", "spawn_failed"],
)
@pytest.mark.asyncio
async def test_a_spawn_that_times_out_or_fails_reports_it(
    tmp_path: Path, raised: Exception, reason: SelfTestFailure
) -> None:
    async def run(**_kwargs: Any) -> SubprocessResult:
        raise raised

    self_tests = _self_tests(_with_roots(_isolation(tmp_path)), run=run)

    await self_tests.run_due([(CONTRACT, "codex_cli")])

    result = self_tests.result(CONTRACT, "codex_cli")
    assert result is not None and result.reason == reason


@pytest.mark.asyncio
async def test_the_self_test_never_runs_while_the_contract_has_a_directive_running(
    tmp_path: Path,
) -> None:
    runs: list[str] = []

    async def run(**kwargs: Any) -> SubprocessResult:
        runs.append(kwargs["argv"][0])
        return SubprocessResult(exit_code=0, stdout="0.141.0", stderr="")

    in_flight = DirectivesInFlight()
    self_tests = _self_tests(_with_roots(_isolation(tmp_path)), in_flight=in_flight, run=run)

    with in_flight.running(CONTRACT):
        await self_tests.run_due([(CONTRACT, "codex_cli")])
    assert runs == []
    assert self_tests.result(CONTRACT, "codex_cli") is None

    # Not held over to the next interval: the first sweep after the Directive runs it.
    await self_tests.run_due([(CONTRACT, "codex_cli")])
    assert runs == ["codex"]


@pytest.mark.asyncio
async def test_a_contract_wiped_mid_sweep_is_neither_recreated_nor_given_a_uid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Sorts after CONTRACT, so the sweep reaches it only after the wipe.
    wiped = "22222222-2222-4222-8222-222222222222"
    isolation = ContractIsolation(
        workspace_root=tmp_path / "workspaces",
        state_dir=tmp_path / "state",
        uid_min=61_000,
        uid_max=61_009,
        max_processes=4096,
        memory_limit_bytes=resource.RLIM_INFINITY,
        can_separate_uids=True,
    )
    # Not root here: let the tree be prepared without the chown a real uid needs.
    monkeypatch.setattr("os.chown", lambda *_args: None)
    _with_roots(isolation, CONTRACT, wiped)
    targets = isolation.harness_roots()

    spawns: list[str] = []

    async def run(**kwargs: Any) -> SubprocessResult:
        spawns.append(kwargs["cwd"].name)
        if len(spawns) == 1:
            # The termination wipe, landing while the sweep awaits the first spawn.
            isolation.wipe(wiped)
        return SubprocessResult(exit_code=0, stdout="codex-cli 0.141.0", stderr="")

    self_tests = _self_tests(isolation, run=run)
    await self_tests.run_due(targets)

    assert spawns == [CONTRACT]
    assert not isolation.contract_dir(wiped).exists()
    assert not isolation.has_uid(wiped)
    assert self_tests.result(wiped, "codex_cli") is None
    assert isolation.harness_roots() == [(CONTRACT, "codex_cli")]


@pytest.mark.asyncio
async def test_a_root_gone_from_the_sweep_drops_its_last_result(tmp_path: Path) -> None:
    async def run(**_kwargs: Any) -> SubprocessResult:
        return SubprocessResult(exit_code=0, stdout="0.141.0", stderr="")

    isolation = _with_roots(_isolation(tmp_path))
    self_tests = _self_tests(isolation, run=run)
    await self_tests.run_due(isolation.harness_roots())
    assert self_tests.result(CONTRACT, "codex_cli") is not None

    isolation.wipe(CONTRACT)
    await self_tests.run_due(isolation.harness_roots())

    assert self_tests.result(CONTRACT, "codex_cli") is None


@pytest.mark.asyncio
async def test_the_self_test_runs_at_most_once_per_interval_per_contract(tmp_path: Path) -> None:
    other = str(uuid4())
    runs: list[str] = []

    async def run(**kwargs: Any) -> SubprocessResult:
        runs.append(str(kwargs["cwd"].name))
        return SubprocessResult(exit_code=0, stdout="0.141.0", stderr="")

    clock = _Clock()
    self_tests = _self_tests(
        _with_roots(_isolation(tmp_path), CONTRACT, other), run=run, monotonic=clock
    )
    targets = [(CONTRACT, "codex_cli"), (other, "codex_cli")]

    await self_tests.run_due(targets)
    clock.now = 14 * 60
    await self_tests.run_due(targets)
    assert runs == [CONTRACT, other]

    clock.now = 15 * 60
    await self_tests.run_due(targets)
    assert runs == [CONTRACT, other, CONTRACT, other]


# ------------------------------------------------------------------ the contract


def test_harness_version_carries_the_descriptor_and_parses_without_it() -> None:
    row = HarnessVersion(
        contract_id=UUID(CONTRACT),
        cli_kind="codex_cli",
        version="0.141.0",
        auth_mode="login",
        auth_modes=["api_key", "subscription"],
        permission_mode="sandbox=workspace-write;approval=never",
        self_test=HarnessSelfTest(ok=False, at=NOW, reason=SelfTestFailure.BELOW_FLOOR),
    )

    assert HarnessVersion.model_validate_json(row.model_dump_json()) == row

    older = {
        "contract_id": CONTRACT,
        "cli_kind": "codex_cli",
        "version": "0.141.0",
        "auth_mode": "login",
    }
    parsed = HarnessVersion.model_validate(older)
    assert (parsed.auth_modes, parsed.permission_mode, parsed.self_test) == (None, None, None)
    # Absent stays absent on the wire, so a control plane on the previous minor parses it.
    assert parsed.model_dump(mode="json") == older


def test_a_self_test_reason_outside_the_codes_is_refused() -> None:
    with pytest.raises(ValueError):
        HarnessSelfTest.model_validate({"ok": False, "at": NOW.isoformat(), "reason": "stderr"})


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


class _Codex:
    auth_modes = frozenset({AuthMode.API_KEY, AuthMode.SUBSCRIPTION})
    host_api_key = False

    def capabilities(self) -> RuntimeCapabilities:
        return RuntimeCapabilities(
            auth_modes=self.auth_modes, permission_mode="sandbox=workspace-write;approval=never"
        )

    async def execute_directive(self, request: DirectiveRequest) -> DirectiveResult:
        raise AssertionError("not run here")


@pytest.mark.asyncio
async def test_the_heartbeat_carries_each_contracts_descriptor_and_self_test(
    tmp_path: Path,
) -> None:
    isolation = _isolation(tmp_path)
    isolation.sandbox(CONTRACT, runtime_kind="codex_cli")
    isolation.sandbox(CONTRACT, runtime_kind="claude_code")
    runtimes: dict[str, AgentRuntime] = {"codex_cli": _Codex()}

    async def run(**_kwargs: Any) -> SubprocessResult:
        return SubprocessResult(exit_code=0, stdout="0.141.0", stderr="")

    self_tests = _self_tests(isolation, served=tuple(runtimes), run=run)
    await self_tests.run_due(isolation.harness_roots())
    registration = _Registration()
    runner_id = uuid4()
    stream = service.ControlPlaneStream(
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
            install_channel=InstallChannel.HELM,
            os="linux 6.8",
            session_kind=SessionKind.CONTAINER,
            store_kind=StoreKind.FILE,
            clis=[CliVersion(cli_kind="codex_cli", version="0.141.0", meets_floor=True)],
        ),
        runtimes=runtimes,
        contracts=isolation,
        self_tests=self_tests,
    )

    await stream.exchange([])

    claude, codex = registration.sent[-1].harnesses
    assert codex == HarnessVersion(
        contract_id=UUID(CONTRACT),
        cli_kind="codex_cli",
        version="0.141.0",
        auth_mode="login",
        auth_modes=["api_key", "subscription"],
        permission_mode="sandbox=workspace-write;approval=never",
        self_test=HarnessSelfTest(ok=True, at=NOW),
    )
    # A harness root this Runner serves no runtime for: no descriptor, and the self-test
    # says why.
    assert (claude.cli_kind, claude.auth_modes, claude.permission_mode) == (
        "claude_code",
        None,
        None,
    )
    assert claude.self_test == HarnessSelfTest(ok=False, at=NOW, reason=SelfTestFailure.NOT_FOUND)
