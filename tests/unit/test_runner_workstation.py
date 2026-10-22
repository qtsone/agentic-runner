"""The workstation Runner (PRD issue 47): packaging, CLIs, storage, attestation, status.

What this pins, per acceptance criterion:

* the three service definitions install a **user-level** service -- no
  ``sudo``, no ``/Library/LaunchDaemons``, no system unit -- and ``status <org>`` reports
  the process, its identity and its heartbeat age;
* ``codex`` and ``claude`` are located on ``PATH``, a missing one the Profile names
  refuses the start, and nothing here opens a file under any harness root (an audit
  hook watches every ``open`` the process makes);
* a value stored through the OS store reads back and is absent from disk; with the store
  unavailable the fallback file is created ``0600``;
* the heartbeat's attestation carries the self-reported facts and nothing content-shaped.

The two-Organisation process test and the sleep test are integration tests
(``tests/integration/test_runner_workstation_processes.py``, ``..._sleep.py``).
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import ValidationError

import agentic_runner
from agentic_runner import cli, service, workstation
from agentic_runner.build import build_id
from agentic_runner.host_store import (
    FileCredentialStore,
    KeychainStore,
    open_workstation_store,
)
from agentic_runner.lifecycle import LifecycleOutbox
from agentic_runner.llm_contracts import save_llm_contracts
from agentic_runner.organisation_name import save_organisation_name
from agentic_runner.pool_key import POOL_KEY_ENV, PoolKeyError, load_pool_key
from agentic_runner.registration import RunnerState, load_state, save_state
from agentic_runner.sealed_box import (
    RecipientKeyStore,
    SealedCredentialError,
    generate_recipient_key,
    open_sealed,
    seal,
)
from agentic_runner.testing import FakeControlPlane
from agentic_runner.workers.claude_sign_in import ClaudeSignIns
from agentic_runner_contracts import __version__ as contracts_version
from agentic_runner_contracts.runner_registration import (
    ContractLlmTerms,
    FloorState,
    HeartbeatAck,
    HeartbeatEnvelope,
    HostAttestation,
    InstallChannel,
    LifecycleKind,
    SessionKind,
    StoreKind,
    SubscriptionRight,
)

REPO = Path(__file__).resolve().parents[2]
CONTROL_PLANE = "http://control-plane.test"


# ------------------------------------------------------------------ fakes


class Commands:
    """The service manager, recorded rather than run."""

    def __init__(self) -> None:
        self.ran: list[list[str]] = []

    def __call__(self, command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        self.ran.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")


def _fake_cli(directory: Path, name: str, version: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(f"#!/bin/sh\necho '{name} {version}'\n")
    path.chmod(0o755)
    return path


def _settings(paths: workstation.RunnerPaths, path: str, **overrides: Any) -> Any:
    return workstation.WorkstationSettings(
        **{
            "control_plane_url": CONTROL_PLANE,
            "temporal_address": "temporal-grpc.test:443",
            "path": path,
            "install_channel": InstallChannel.HOMEBREW,
            **overrides,
        }
    )


@pytest.fixture
def bin_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "bin"
    _fake_cli(directory, "codex", "codex-cli 0.141.2")
    _fake_cli(directory, "claude", "2.1.279 (Claude Code)")
    return directory


# ------------------------------------------------------------------ packaging


def _all_commands(definition: workstation.ServiceDefinition) -> list[list[str]]:
    return definition.install + definition.start + definition.stop + definition.restart


@pytest.mark.parametrize("platform", ["darwin", "linux", "win32"])
def test_every_service_definition_is_user_level(tmp_path: Path, platform: str) -> None:
    home = tmp_path / "home"
    paths = workstation.RunnerPaths(root=home / "state", name="acme")
    definition = workstation.service_definition(
        paths,
        _settings(paths, "/usr/bin:/bin"),
        platform=platform,
        home=home,
        user="dev",
        uid=501,
        command=["/opt/homebrew/bin/agentic-runner", "run", "--name", "acme"],
    )

    # Under the user's home, never a system location.
    assert definition.path.is_relative_to(home)
    text = definition.content.decode("utf-16" if platform == "win32" else "utf-8")
    for forbidden in ("/Library/LaunchDaemons", "/etc/systemd", "HighestAvailable", "sudo"):
        assert forbidden not in text
    for command in _all_commands(definition):
        assert command[0] != "sudo"
        assert "--system" not in command

    if platform == "darwin":
        assert definition.path == home / "Library/LaunchAgents/agentic-runner.acme.plist"
        agent = plistlib.loads(definition.content)
        assert agent["Label"] == "agentic-runner.acme"
        assert agent["ProgramArguments"][1:4] == ["run", "--name", "acme"]
        assert agent["EnvironmentVariables"] == {"PATH": "/usr/bin:/bin"}
        # The per-user GUI domain -- a LaunchAgent -- not the `system` domain.
        assert definition.start == [["launchctl", "bootstrap", "gui/501", str(definition.path)]]
    elif platform == "linux":
        assert definition.path == home / ".config/systemd/user/agentic-runner-acme.service"
        assert all(c[:2] == ["systemctl", "--user"] for c in definition.start + definition.stop)
        assert ["loginctl", "enable-linger", "dev"] in definition.install
        assert "WantedBy=default.target" in text
    else:
        assert "<LogonType>InteractiveToken</LogonType>" in text
        assert "<RunLevel>LeastPrivilege</RunLevel>" in text
        assert "<UserId>dev</UserId>" in text


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_the_service_manager_waits_out_the_drain(tmp_path: Path, platform: str) -> None:
    paths = workstation.RunnerPaths(root=tmp_path / "state", name="acme")
    definition = workstation.service_definition(
        paths, _settings(paths, "/usr/bin:/bin"), platform=platform, home=tmp_path, uid=501
    )

    # QTS-2166: launchd's default ExitTimeOut is 20 s, systemd's TimeoutStopSec 90 s.
    if platform == "darwin":
        assert plistlib.loads(definition.content)["ExitTimeOut"] == 2100
    else:
        assert "\nTimeoutStopSec=2100\n" in definition.content.decode()
    assert workstation.DRAIN_TIMEOUT_SECONDS == 2100


def test_the_cli_floors_are_the_versions_the_runner_image_pins() -> None:
    dockerfile = (REPO / "Dockerfile.runner").read_text()

    assert f"CODEX_CLI_VERSION={workstation.CLI_FLOORS['codex']}" in dockerfile
    assert f"CLAUDE_CODE_VERSION={workstation.CLI_FLOORS['claude']}" in dockerfile


@pytest.mark.asyncio
async def test_install_registers_writes_the_launch_agent_and_status_reports_it(
    tmp_path: Path, bin_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    paths = workstation.RunnerPaths(root=tmp_path / "state", name="acme")
    plane = FakeControlPlane(host_party="user")
    commands = Commands()

    async with httpx.AsyncClient(transport=plane.transport()) as http:
        lines = await workstation.install(
            paths,
            _settings(paths, str(bin_dir)),
            registration_token="registration-token-of-sixteen-plus",
            platform="darwin",
            home=home,
            run=commands,
            http_client=http,
        )

    [bootstrap] = plane.bootstraps
    assert bootstrap.isolation_mode == "none"
    plist = home / "Library/LaunchAgents/agentic-runner.acme.plist"
    assert plist.is_file()
    assert commands.ran == [["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)]]
    assert any("registered" in line for line in lines)
    # QTS-1314: the dogfood install stopped at `started`, and the person was stuck.
    assert lines[-2].startswith("started")
    assert "/me/runners" in lines[-1] and "/me/work/new" in lines[-1]
    assert paths.state_dir.stat().st_mode & 0o777 == 0o700
    assert [event.kind for event in LifecycleOutbox(paths.state_dir).pending()] == [
        LifecycleKind.INSTALL
    ]

    now = datetime.now(UTC)
    status = workstation.status_lines(paths, now=now)
    assert "not running" in status[1]
    assert "on runner." in status[2] and "in org-" in status[2]
    assert status[3].endswith("never")
    assert len(status) == 4, "no next step while the Runner is not running"

    paths.pidfile.write_text(str(os.getpid()))
    paths.heartbeat_stamp.touch()
    os.utime(paths.heartbeat_stamp, (now.timestamp() - 42, now.timestamp() - 42))
    status = workstation.status_lines(paths, now=now)
    assert status[1].endswith(f"running, pid {os.getpid()}")
    assert status[3].endswith("42s ago")
    assert status[4] == lines[-1]

    # The CLI verb prints the same lines.
    assert cli.main(["status", "acme", "--root", str(paths.root)]) == 0
    assert "heartbeat" in capsys.readouterr().out

    paths.pidfile.unlink()
    workstation.stop(paths, platform="darwin", home=home, run=commands)
    assert commands.ran[-1] == ["launchctl", "bootout", f"gui/{os.getuid()}/agentic-runner.acme"]


@pytest.mark.asyncio
async def test_login_makes_the_runner_the_persons_and_installs_its_login_agent(
    tmp_path: Path, bin_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """QTS-2252: `install` with a device flow in place of the pasted token."""

    monkeypatch.delenv("AGENTIC_RUNNER_REGISTRATION_TOKEN", raising=False)
    home = tmp_path / "home"
    paths = workstation.RunnerPaths(root=tmp_path / "state", name="laptop")
    plane = FakeControlPlane(confirm_logins=True)
    commands = Commands()
    shown: list[str] = []
    opened: list[str] = []

    async with httpx.AsyncClient(transport=plane.transport()) as http:
        lines = await workstation.login(
            paths,
            _settings(paths, str(bin_dir)),
            say=shown.append,
            open_browser=opened.append,
            platform="darwin",
            home=home,
            run=commands,
            http_client=http,
        )

    assert plane.bootstraps[0].token.startswith("device-code-"), "no token pasted"
    [request] = plane.logins
    assert (request.name, request.isolation_mode) == ("laptop", "none")
    assert opened == ["https://console.test/device?code=CODE-0001"]
    plist = home / "Library/LaunchAgents/agentic-runner.laptop.plist"
    assert commands.ran == [["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)]]
    assert lines[0].startswith("logged in laptop")
    assert lines[-1].startswith("started")
    status = workstation.status_lines(paths)
    assert "in each Organisation it serves" in status[2]


def test_install_names_its_argument_as_the_runners_name(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """QTS-1773: the name typed on /me/runners read, on the machine, as an Organisation."""

    with pytest.raises(SystemExit):
        cli.main(["install", "--help"])

    usage = capsys.readouterr().out
    assert "install [-h]" in usage and " name" in usage
    assert "this Runner's name" in usage and "rganisation" not in usage


def test_an_install_made_before_the_runner_name_keeps_running(
    tmp_path: Path, bin_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its settings file still carries `org`, and its login agent still runs `run --org`:
    `uv tool upgrade` rewrites neither, and the Runner starts as it did, under that name."""

    paths = workstation.RunnerPaths(root=tmp_path / "state", name="cucubau")
    paths.create()
    paths.settings_file.write_text(
        json.dumps(
            {
                "org": "cucubau",
                "control_plane_url": CONTROL_PLANE,
                "path": str(bin_dir),
                "install_channel": "manual",
            }
        )
    )
    started: list[tuple[workstation.RunnerPaths, bool]] = []

    def run_process(run_paths: workstation.RunnerPaths, *, login_agent: bool) -> int:
        started.append((run_paths, login_agent))
        return 0

    monkeypatch.setattr(workstation, "run_process", run_process)

    argv = ["run", "--org", "cucubau", "--root", str(paths.root), "--login-agent"]
    assert cli.main(argv) == 0

    assert started == [(paths, True)]
    assert workstation.WorkstationSettings.load(paths).control_plane_url == CONTROL_PLANE


# ------------------------------------------------------------------ list, and no name to type


def _installed(
    root: Path, name: str, *, organisation: UUID | None = None
) -> workstation.RunnerPaths:
    paths = workstation.RunnerPaths(root=root, name=name)
    paths.create()
    _settings(paths, "/usr/bin:/bin").save(paths)
    if organisation is not None:
        save_state(
            paths.state_dir,
            RunnerState(
                runner_id=uuid4(),
                identity_id="id",
                private_key_pem="unused",
                temporal_namespace=f"org-{organisation}",
                task_queue="runner.x",
            ),
        )
    return paths


def test_list_prints_one_row_per_runner_installed_here(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """QTS-1774: five installs, five rows, with what each one is and whether it runs."""

    root = tmp_path / "state"
    acme, billing = uuid4(), uuid4()
    contract, other = uuid4(), uuid4()
    running = _installed(root, "alice-laptop", organisation=acme)
    save_llm_contracts(
        running.state_dir,
        [ContractLlmTerms(contract_id=contract, subscription=SubscriptionRight.ALLOWED)],
    )
    running.started_version.write_text("3.28.0")
    running.pidfile.write_text(str(os.getpid()))
    pool = _installed(root, "acme-pool", organisation=acme)
    save_llm_contracts(
        pool.state_dir,
        [
            ContractLlmTerms(contract_id=identity, subscription=SubscriptionRight.NO_RIGHT)
            for identity in (contract, other)
        ],
    )
    _installed(root, "billing", organisation=billing).started_version.write_text("3.27.0")
    _installed(root, "cucubau", organisation=billing)
    _installed(root, "never-registered")
    # Moved aside before a new token, as the guide says: no longer a Runner here.
    (root / "acme.organisation").mkdir()
    (root / "acme.organisation" / "workstation.json").write_text("{}")

    assert cli.main(["list", "--root", str(root)]) == 0

    header, *rows = capsys.readouterr().out.splitlines()
    assert header.split() == ["NAME", "ORGANISATION", "CONTRACT", "CONTROL", "PLANE", "VERSION"] + [
        "LOGIN",
        "AGENT",
    ]
    assert [row.split() for row in rows] == [
        ["acme-pool", str(acme)[:8], "2", "Contracts", CONTROL_PLANE, "-", "stopped"],
        ["alice-laptop", str(acme)[:8], str(contract)[:8], CONTROL_PLANE, "3.28.0", "running"],
        ["billing", str(billing)[:8], "-", CONTROL_PLANE, "3.27.0", "stopped"],
        ["cucubau", str(billing)[:8], "-", CONTROL_PLANE, "-", "stopped"],
        ["never-registered", "-", "-", CONTROL_PLANE, "-", "stopped"],
    ]
    # Columns line up, so the table reads as one.
    assert len({row.index(CONTROL_PLANE) for row in [*rows]}) == 1


def test_list_shows_why_an_install_cannot_be_read_and_still_lists_the_others(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """QTS-1910: one unsafe or invalid install used to end `list` in a traceback."""

    root = tmp_path / "state"
    organisation = uuid4()
    _installed(root, "good", organisation=organisation)
    unsafe = _installed(root, "open-to-group", organisation=organisation)
    unsafe.state_dir.chmod(0o755)
    invalid = _installed(root, "invalid")
    invalid.settings_file.write_text('{"path": "/usr/bin"}')

    assert cli.main(["list", "--root", str(root)]) == 0

    _header, *rows = capsys.readouterr().out.splitlines()
    assert [row.split()[:5] for row in rows] == [
        ["good", str(organisation)[:8], "-", CONTROL_PLANE, "-"],
        ["invalid", "-", "-", "-", "-"],
        ["open-to-group", "-", "-", "-", "-"],
    ]
    assert rows[0].endswith("stopped")
    assert "unreadable: 2 validation errors for WorkstationSettings" in rows[1]
    assert f"unreadable: {unsafe.state_dir} is mode 0755, open to group or others" in rows[2]


def test_list_names_the_organisation_and_contract_once_an_ack_named_them(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """QTS-1909: names from a control plane on 3.29, the ids from one below it. The name
    is printed without its control characters: someone else typed it."""

    root = tmp_path / "state"
    acme, contract = uuid4(), uuid4()
    named = _installed(root, "alice-laptop", organisation=acme)
    save_organisation_name(named.state_dir, "Acme\x1b]0;owned\x07")
    save_llm_contracts(
        named.state_dir,
        [
            ContractLlmTerms(
                contract_id=contract, subscription=SubscriptionRight.ALLOWED, contract_name="Web"
            )
        ],
    )
    unnamed = _installed(root, "billing", organisation=acme)
    save_llm_contracts(
        unnamed.state_dir,
        [ContractLlmTerms(contract_id=contract, subscription=SubscriptionRight.ALLOWED)],
    )

    assert cli.main(["list", "--root", str(root)]) == 0

    _, *rows = capsys.readouterr().out.splitlines()
    assert [row.split()[:3] for row in rows] == [
        ["alice-laptop", "Acme]0;owned", "Web"],
        ["billing", str(acme)[:8], str(contract)[:8]],
    ]


def test_list_on_a_machine_with_no_runner_says_where_it_looked(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["list", "--root", str(tmp_path / "absent")]) == 0
    assert capsys.readouterr().out.strip() == (
        f"no workstation Runner is installed under {tmp_path / 'absent'}"
    )


def test_the_runner_writes_the_version_it_started_on(
    tmp_path: Path, bin_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = workstation.RunnerPaths(root=tmp_path / "state", name="acme")
    paths.create()
    _settings(paths, str(bin_dir)).save(paths)
    monkeypatch.setattr(os, "environ", dict(os.environ))

    async def run(**_: Any) -> int:
        return 0

    monkeypatch.setattr(service, "run", run)
    monkeypatch.setattr(workstation, "open_workstation_store", lambda *_a, **_k: _FileStore())

    assert workstation.run_process(paths, login_agent=True) == 0
    assert paths.started_version.read_text() == agentic_runner.__version__


@pytest.mark.parametrize(
    ("service_exit", "agent_exit"), [(service.REVOKED_EXIT_CODE, 0), (1, 1), (0, 0)]
)
def test_a_revoked_runner_exits_clean_so_its_login_agent_stays_down(
    tmp_path: Path,
    bin_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    service_exit: int,
    agent_exit: int,
) -> None:
    # Every service manager here restarts a non-zero exit; a revoked Runner restarted that
    # way heartbeated into a 403 every ~10 s (QTS-2207). Any other failure still restarts.
    paths = workstation.RunnerPaths(root=tmp_path / "state", name="acme")
    paths.create()
    _settings(paths, str(bin_dir)).save(paths)
    monkeypatch.setattr(os, "environ", dict(os.environ))

    async def run(**_: Any) -> int:
        return service_exit

    monkeypatch.setattr(service, "run", run)
    monkeypatch.setattr(workstation, "open_workstation_store", lambda *_a, **_k: _FileStore())

    assert workstation.run_process(paths, login_agent=True) == agent_exit


class _FileStore:
    kind = StoreKind.FILE

    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def put(self, reference: str, value: str) -> None:
        self.values[reference] = value


def test_with_one_runner_installed_no_command_needs_its_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    only = _installed(root, "alice-laptop")
    started: list[workstation.RunnerPaths] = []
    monkeypatch.setattr(workstation, "start", started.append)
    store = _FileStore()
    monkeypatch.setattr(cli, "open_workstation_store", lambda *_a, **_k: store)
    monkeypatch.setattr(sys, "stdin", io.StringIO("sk-typed\n"))

    assert cli.main(["start", "--root", str(root)]) == 0
    assert cli.main(["credential", "set", "OPENAI_API_KEY", "--root", str(root)]) == 0

    assert started == [only]
    assert store.values == {"OPENAI_API_KEY": "sk-typed"}


@pytest.mark.parametrize(
    "argv",
    [["start"], ["stop"], ["status"], ["credential", "set", "OPENAI_API_KEY"]],
)
def test_with_several_runners_installed_a_command_names_them_and_points_to_list(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
) -> None:
    root = tmp_path / "state"
    _installed(root, "alice-laptop")
    _installed(root, "billing")
    monkeypatch.setattr(workstation, "start", _not_driven)
    monkeypatch.setattr(workstation, "stop", _not_driven)
    monkeypatch.setattr(cli, "_in_container", lambda: False)

    assert cli.main([*argv, "--root", str(root)]) != 0

    error = capsys.readouterr().err
    assert "alice-laptop, billing" in error and "`agentic-runner list`" in error


def _not_driven(_paths: workstation.RunnerPaths) -> None:
    raise AssertionError("no Runner was named")


# ------------------------------------------------------------------ restart (QTS-2158)


@pytest.mark.parametrize(
    ("platform", "restart"),
    [
        (
            "darwin",
            [
                ["launchctl", "kill", "SIGTERM", "gui/501/agentic-runner.acme"],
                ["launchctl", "kickstart", "-k", "gui/501/agentic-runner.acme"],
            ],
        ),
        ("linux", [["systemctl", "--user", "restart", "agentic-runner-acme.service"]]),
        (
            "win32",
            [
                ["schtasks", "/End", "/TN", "\\agentic-runner\\acme"],
                ["schtasks", "/Run", "/TN", "\\agentic-runner\\acme"],
            ],
        ),
    ],
)
def test_restart_runs_the_service_managers_own_restart_and_waits_for_the_new_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str, restart: list[list[str]]
) -> None:
    """After `uv tool upgrade` the login agent still runs the old code (owner, QTS-1320)."""

    paths = _installed(tmp_path / "state", "acme")
    paths.pidfile.write_text("111")
    paths.started_version.write_text("3.28.0")
    monkeypatch.setattr(workstation.os, "getuid", lambda: 501)
    live = {111}
    monkeypatch.setattr(workstation, "process_id", lambda p: _live_pid(p, live))
    commands = Commands()
    polls: list[float] = []

    def service_manager(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        live.discard(111)
        return commands(command, **kwargs)

    def sleep(seconds: float) -> None:
        # The old process drains and the new one writes its version, then its pidfile.
        polls.append(seconds)
        live.add(222)
        paths.started_version.write_text("3.29.0")
        paths.pidfile.write_text("222")

    pid, version = workstation.restart(
        paths, platform=platform, home=tmp_path / "home", run=service_manager, sleep=sleep
    )

    assert commands.ran == restart
    assert (pid, version) == (222, "3.29.0")
    assert polls == [0.5]


def _live_pid(paths: workstation.RunnerPaths, live: set[int]) -> int | None:
    pid = int(paths.pidfile.read_text())
    return pid if pid in live else None


def test_restart_says_so_when_no_new_process_comes_up(tmp_path: Path) -> None:
    paths = _installed(tmp_path / "state", "acme")
    ticks = iter(range(100))

    with pytest.raises(RuntimeError, match=r"within 60s; see .*runner\.log"):
        workstation.restart(
            paths,
            platform="linux",
            home=tmp_path / "home",
            run=Commands(),
            sleep=lambda _s: None,
            clock=lambda: float(next(ticks)) * 10,
        )


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_restart_waits_for_the_new_process_only_once_the_drain_has_ended(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    """`kickstart -k` and `systemctl restart` both return only when the old process has
    exited (CI probe, QTS-2172), so a drain spends none of the 60 s that `restart` gives the
    new process to come up."""

    paths = _installed(tmp_path / "state", "acme")
    paths.pidfile.write_text("111")
    monkeypatch.setattr(workstation.os, "getuid", lambda: 501)
    live = {111}
    monkeypatch.setattr(workstation, "process_id", lambda p: _live_pid(p, live))
    now = [0.0]
    commands = Commands()

    def service_manager(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        now[0] += workstation.DRAIN_TIMEOUT_SECONDS
        live.discard(111)
        return commands(command, **kwargs)

    def sleep(seconds: float) -> None:
        now[0] += seconds
        live.add(222)
        paths.started_version.write_text("3.29.0")
        paths.pidfile.write_text("222")

    pid, version = workstation.restart(
        paths,
        platform=platform,
        home=tmp_path / "home",
        run=service_manager,
        sleep=sleep,
        clock=lambda: now[0],
    )

    assert (pid, version) == (222, "3.29.0")


def _draining(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, drain: float
) -> tuple[workstation.RunnerPaths, Commands, dict[str, Any]]:
    """A Runner whose process takes ``drain`` seconds to exit once it gets SIGTERM."""

    paths = _installed(tmp_path / "state", "acme")
    paths.pidfile.write_text("111")
    monkeypatch.setattr(workstation.os, "getuid", lambda: 501)
    now = [0.0]
    signalled: list[float] = []
    commands = Commands()

    def process_id(_paths: workstation.RunnerPaths) -> int | None:
        return 111 if not signalled or now[0] < signalled[0] + drain else None

    def service_manager(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if command[:2] == ["launchctl", "kill"]:
            signalled.append(now[0])
        return commands(command, **kwargs)

    def sleep(seconds: float) -> None:
        now[0] += seconds

    monkeypatch.setattr(workstation, "process_id", process_id)
    driver = {
        "platform": "darwin",
        "home": tmp_path / "home",
        "run": service_manager,
        "sleep": sleep,
        "clock": lambda: now[0],
    }
    return paths, commands, {"driver": driver, "now": now}


def test_stop_on_macos_lets_the_drain_run_past_launchds_60_s(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """launchd SIGKILLs its own stop after 60 s whatever ExitTimeOut says (QTS-2178), so the
    Runner gets SIGTERM directly and launchd's `bootout` comes only after it has exited."""

    paths, commands, fake = _draining(tmp_path, monkeypatch, drain=150)

    workstation.stop(paths, **fake["driver"])

    target = "gui/501/agentic-runner.acme"
    assert commands.ran == [
        ["launchctl", "kill", "SIGTERM", target],
        ["launchctl", "bootout", target],
    ]
    assert fake["now"][0] >= 150


def test_stop_on_macos_hands_a_drain_past_the_budget_to_launchd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, commands, fake = _draining(
        tmp_path, monkeypatch, drain=workstation.DRAIN_TIMEOUT_SECONDS + 600
    )

    workstation.stop(paths, **fake["driver"])

    assert commands.ran[-1] == ["launchctl", "bootout", "gui/501/agentic-runner.acme"]
    assert fake["now"][0] == pytest.approx(workstation.DRAIN_TIMEOUT_SECONDS, abs=1)


def test_stop_sends_no_signal_to_a_runner_that_is_not_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _installed(tmp_path / "state", "acme")
    monkeypatch.setattr(workstation.os, "getuid", lambda: 501)
    commands = Commands()

    workstation.stop(paths, platform="darwin", home=tmp_path / "home", run=commands)

    assert commands.ran == [["launchctl", "bootout", "gui/501/agentic-runner.acme"]]


def test_stop_on_linux_leaves_the_drain_to_systemd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`systemctl stop` honours TimeoutStopSec in full (CI probe, QTS-2172)."""

    paths = _installed(tmp_path / "state", "acme")
    paths.pidfile.write_text("111")
    monkeypatch.setattr(workstation, "process_id", lambda _p: 111)
    commands = Commands()

    workstation.stop(paths, platform="linux", home=tmp_path / "home", run=commands)

    assert commands.ran == [
        ["systemctl", "--user", "disable", "--now", "agentic-runner-acme.service"]
    ]


@pytest.fixture
def restarted(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    names: list[str] = []

    def restart(paths: workstation.RunnerPaths) -> tuple[int, str]:
        names.append(paths.name)
        return 4000 + len(names), "3.29.0"

    monkeypatch.setattr(workstation, "restart", restart)
    monkeypatch.setattr(workstation, "process_id", lambda p: None if p.name == "parked" else 1)
    return names


@pytest.mark.parametrize("argv", [["restart", "billing"], ["restart", "--name", "billing"]])
def test_restart_takes_the_runners_name_either_way_and_prints_the_new_pid_and_version(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], restarted: list[str], argv: list[str]
) -> None:
    root = tmp_path / "state"
    _installed(root, "alice-laptop")
    _installed(root, "billing")

    assert cli.main([*argv, "--root", str(root)]) == 0

    assert restarted == ["billing"]
    assert capsys.readouterr().out == "restarted billing: pid 4001, agentic-runner 3.29.0\n"


def test_restart_all_restarts_every_running_runner_and_leaves_a_stopped_one_stopped(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], restarted: list[str]
) -> None:
    root = tmp_path / "state"
    for name in ("alice-laptop", "billing", "parked"):
        _installed(root, name)

    assert cli.main(["restart", "--all", "--root", str(root)]) == 0

    assert restarted == ["alice-laptop", "billing"]
    assert capsys.readouterr().out.splitlines() == [
        "restarted alice-laptop: pid 4001, agentic-runner 3.29.0",
        "restarted billing: pid 4002, agentic-runner 3.29.0",
        "parked is stopped; `agentic-runner start parked` starts it",
    ]


def test_restart_of_a_name_not_installed_exits_2_and_lists_the_installed_names(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], restarted: list[str]
) -> None:
    root = tmp_path / "state"
    _installed(root, "alice-laptop")
    _installed(root, "billing")

    assert cli.main(["restart", "acme", "--root", str(root)]) == 2

    assert restarted == []
    assert capsys.readouterr().err.strip() == (
        f"no Runner named 'acme' is installed under {root}; installed: alice-laptop, billing"
    )


@pytest.mark.parametrize(
    ("argv", "error"),
    [
        (["restart"], "alice-laptop, billing: name one, or restart --all"),
        (["restart", "billing", "--all"], "a name or --all, not both"),
    ],
)
def test_restart_with_several_runners_needs_one_name_or_all(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    restarted: list[str],
    argv: list[str],
    error: str,
) -> None:
    root = tmp_path / "state"
    _installed(root, "alice-laptop")
    _installed(root, "billing")

    assert cli.main([*argv, "--root", str(root)]) == 2

    assert restarted == []
    assert error in capsys.readouterr().err


def test_restart_of_a_stopped_runner_by_name_fails_and_says_how_to_start_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], restarted: list[str]
) -> None:
    root = tmp_path / "state"
    _installed(root, "parked")

    assert cli.main(["restart", "--root", str(root)]) == 1

    assert restarted == []
    assert "`agentic-runner start parked`" in capsys.readouterr().out


def test_sign_in_needs_no_name_with_one_runner_and_lists_them_with_several(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The acceptance criterion: `agentic-runner sign-in claude`, nothing else typed."""

    root = tmp_path / "state"
    _installed(root, "alice-laptop")
    signed: list[str | None] = []

    async def sign_in(which: str | None, *, store: Any, method: str) -> int:
        signed.append(os.environ["AGENTIC_RUNNER_STATE_DIR"])
        return 0

    monkeypatch.setattr(service, "sign_in", sign_in)
    monkeypatch.setattr(os, "isatty", lambda _fd: True)
    monkeypatch.setenv(workstation.ROOT_ENV, str(root))
    # `sign-in` exports the Runner's environment into its own; this one variable is enough
    # to tell which Runner it chose, and setting it here first lets the fixture restore it.
    monkeypatch.setenv("AGENTIC_RUNNER_STATE_DIR", "")
    monkeypatch.setattr(
        workstation,
        "run_environment",
        lambda paths, _settings: {"AGENTIC_RUNNER_STATE_DIR": str(paths.state_dir)},
    )
    monkeypatch.setattr(cli, "_in_container", lambda: False)
    monkeypatch.setattr(cli, "open_workstation_store", lambda *_a, **_k: _FileStore())

    assert cli.main(["sign-in", "claude"]) == 0
    assert signed == [str(root / "alice-laptop")]

    _installed(root, "billing")
    assert cli.main(["sign-in", "claude"]) == 2
    error = capsys.readouterr().err
    assert "alice-laptop, billing: name one with --name" in error
    assert "`agentic-runner list`" in error
    assert len(signed) == 1


def test_a_subscription_sign_in_needs_only_the_workstation_run_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real `run_environment`, nothing else: it once lacked the callback URL (QTS-2157)."""

    root = tmp_path / "state"
    paths = _installed(root, "alice-laptop")
    save_state(
        paths.state_dir,
        RunnerState(
            runner_id=uuid4(),
            identity_id="id",
            private_key_pem="unused",
            temporal_namespace="org-x",
            task_queue="runner.x",
            host_party="user",
        ),
    )
    contract = uuid4()
    save_llm_contracts(
        paths.state_dir,
        [ContractLlmTerms(contract_id=contract, subscription=SubscriptionRight.ALLOWED)],
    )
    # `sign-in` exports the Runner's environment into its own; set-then-delete records each
    # name so the fixture restores it, and leaves the test with exactly that environment.
    for name in [
        *workstation.run_environment(paths, _settings(paths, "")),
        "INTERNAL_FASTAPI_BASE_URL",
    ]:
        monkeypatch.setenv(name, os.environ.get(name, ""))
        if name != "PATH":
            monkeypatch.delenv(name)
    called_back: list[str] = []

    async def claude(_self: ClaudeSignIns, contract_id: str, *, method: str) -> int:
        called_back.append(str(service.get_worker_settings().INTERNAL_FASTAPI_BASE_URL))
        return 0

    monkeypatch.setattr(ClaudeSignIns, "sign_in", claude)
    monkeypatch.setattr(service, "can_separate_uids", lambda: False)
    monkeypatch.setattr(service.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("builtins.input", lambda _prompt: "s")
    monkeypatch.setattr(os, "isatty", lambda _fd: True)
    monkeypatch.setenv(workstation.ROOT_ENV, str(root))
    monkeypatch.setattr(cli, "_in_container", lambda: False)
    monkeypatch.setattr(cli, "open_workstation_store", lambda *_a, **_k: _FileStore())

    assert cli.main(["sign-in", "claude", "--name", "alice-laptop"]) == 0
    assert called_back == [f"{CONTROL_PLANE}/"]


def test_a_bare_run_on_a_machine_with_installed_runners_says_to_start_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Owner note 3 (QTS-1774): `run` needs parameters an installed Runner never types."""

    root = tmp_path / "state"
    _installed(root, "alice-laptop")
    monkeypatch.delenv(service.REGISTRATION_TOKEN_ENV, raising=False)
    monkeypatch.delenv(service.AGENT_TOKEN_ENV, raising=False)
    monkeypatch.setenv(workstation.ROOT_ENV, str(root))
    monkeypatch.setenv("AGENTIC_RUNNER_STATE_DIR", str(tmp_path / "unregistered"))
    monkeypatch.setenv("AGENTIC_RUNNER_CONFIG", str(tmp_path / "absent.toml"))
    monkeypatch.setattr(cli, "_in_container", lambda: False)
    monkeypatch.setattr(service, "run", _not_run)

    assert cli.main(["run"]) == 2
    error = capsys.readouterr().err
    assert "alice-laptop" in error and "`agentic-runner start <name>`" in error


async def _not_run(**_: Any) -> int:
    raise AssertionError("a bare run must not register from an empty environment")


@pytest.mark.asyncio
async def test_reinstalling_over_a_registered_state_dir_says_the_token_was_not_used(
    tmp_path: Path, bin_dir: Path
) -> None:
    paths = workstation.RunnerPaths(root=tmp_path / "state", name="acme")
    plane = FakeControlPlane(host_party="organisation")

    async def install(token: str) -> list[str]:
        async with httpx.AsyncClient(transport=plane.transport()) as http:
            return await workstation.install(
                paths,
                _settings(paths, str(bin_dir)),
                registration_token=token,
                platform="darwin",
                home=tmp_path / "home",
                run=Commands(),
                http_client=http,
            )

    first = await install("organisation-hosted-token-1")
    assert not any(line.startswith("token") for line in first)

    second = await install("user-hosted-token-of-sixteen")

    assert len(plane.bootstraps) == 1, "the pasted token never reaches the control plane"
    assert second[0].startswith("already registered")
    assert second[1].startswith("token          not used")
    assert str(paths.state_dir) in second[1] and "Switch to a new token" in second[1]


@pytest.mark.asyncio
async def test_two_installs_on_one_machine_hold_their_own_identity_and_recipient_key(
    tmp_path: Path, bin_dir: Path
) -> None:
    """ADR-0022 §37: a value sealed to one Runner on the machine opens on no other."""

    root = tmp_path / "state"
    first = workstation.RunnerPaths(root=root, name="acme")
    second = workstation.RunnerPaths(root=root, name="acme-billing")
    plane = FakeControlPlane(host_party="user")

    async def register(paths: workstation.RunnerPaths, token: str) -> None:
        async with httpx.AsyncClient(transport=plane.transport()) as http:
            await workstation.install(
                paths,
                _settings(paths, str(bin_dir)),
                registration_token=token,
                platform="darwin",
                home=tmp_path / "home",
                run=Commands(),
                http_client=http,
            )

    await register(first, "token-for-the-acme-contract")
    await register(second, "token-for-the-billing-contract")

    states = [load_state(paths.state_dir) for paths in (first, second)]
    assert states[0] and states[1] and states[0].runner_id != states[1].runner_id

    [acme_key, billing_key] = [bootstrap.recipient_key for bootstrap in plane.bootstraps]
    assert acme_key.key_id != billing_key.key_id
    acme_pair, billing_pair = (RecipientKeyStore(p.state_dir).current() for p in (first, second))
    assert acme_pair.public_key == acme_key.public_key
    assert billing_pair.public_key == billing_key.public_key
    binding = b"contract|slot"
    sealed = seal(public_key=acme_key.public_key, binding=binding, plaintext="sk-acme")
    assert open_sealed(private_key=acme_pair.private_key, binding=binding, ciphertext=sealed) == (
        "sk-acme"
    )
    with pytest.raises(SealedCredentialError):
        open_sealed(private_key=billing_pair.private_key, binding=binding, ciphertext=sealed)


def test_the_next_step_follows_who_hosts_the_runner() -> None:
    [user] = workstation.next_step_lines("user")
    assert "/me/work/new" in user and "a Runner you host" in user
    [organisation] = workstation.next_step_lines("organisation")
    assert "hosted by the organisation" in organisation and "/me/" not in organisation
    # A control plane older than issue 42 names no host party; nothing to point at.
    assert workstation.next_step_lines("") == []


def test_the_token_prompt_names_both_places_a_token_comes_from(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prompts: list[str] = []

    def refuse(prompt: str) -> str:
        prompts.append(prompt)
        raise KeyboardInterrupt

    monkeypatch.delenv(cli.AGENT_TOKEN_ENV, raising=False)
    monkeypatch.setattr(cli.getpass, "getpass", refuse)
    with pytest.raises(KeyboardInterrupt):
        cli.main(
            [
                "install",
                "acme",
                "--control-plane",
                CONTROL_PLANE,
                "--temporal-address",
                "temporal.test:443",
                "--root",
                str(tmp_path),
            ]
        )

    [prompt] = prompts
    assert "/me/runners" in prompt and "Organisation Admin" in prompt


def test_a_reinstall_keeps_the_acp_cli_kinds_the_last_install_was_given(
    tmp_path: Path, bin_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """QTS-1355: `ACP_CLI_KINDS` hand-added to the plist was dropped by the next `install`."""

    paths = workstation.RunnerPaths(root=tmp_path / "state", name="acme")
    installed: list[workstation.WorkstationSettings] = []

    async def install(
        org_paths: workstation.RunnerPaths, settings: workstation.WorkstationSettings, **_: Any
    ) -> list[str]:
        org_paths.create()
        settings.save(org_paths)
        installed.append(settings)
        return []

    monkeypatch.setenv(cli.AGENT_TOKEN_ENV, "agent-token-of-sixteen-plus")
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setattr(cli.workstation, "install", install)

    def reinstall(*flags: str) -> int:
        return cli.main(
            [
                "install",
                "acme",
                "--control-plane",
                CONTROL_PLANE,
                "--temporal-address",
                "temporal.test:443",
                "--root",
                str(paths.root),
                "--no-start",
                *flags,
            ]
        )

    assert reinstall("--acp-cli-kinds", " claude_code,codex_cli,claude_code ") == 0
    assert reinstall() == 0
    assert installed[-1].acp_cli_kinds == "claude_code,codex_cli"
    environment = workstation.run_environment(paths, workstation.WorkstationSettings.load(paths))
    assert environment["ACP_CLI_KINDS"] == "claude_code,codex_cli"

    assert reinstall("--acp-cli-kinds", "") == 0
    assert "ACP_CLI_KINDS" not in workstation.run_environment(paths, installed[-1])

    # The Runner drops a kind with no pinned bridge silently; `install` must not.
    assert reinstall("--acp-cli-kinds", "claude_cli") == 2
    assert len(installed) == 3


def test_install_needs_no_temporal_address_and_keeps_one_given_as_a_fallback(
    tmp_path: Path, bin_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """QTS-1540: the Runner takes its Temporal address from registration (ADR-0022 §4)."""

    paths = workstation.RunnerPaths(root=tmp_path / "state", name="acme")
    installed: list[workstation.WorkstationSettings] = []

    async def install(
        org_paths: workstation.RunnerPaths, settings: workstation.WorkstationSettings, **_: Any
    ) -> list[str]:
        installed.append(settings)
        return []

    monkeypatch.setenv(cli.AGENT_TOKEN_ENV, "agent-token-of-sixteen-plus")
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setattr(cli.workstation, "install", install)
    flags = ["install", "acme", "--control-plane", CONTROL_PLANE, "--root", str(paths.root)]

    assert cli.main(flags) == 0
    assert installed[-1].temporal_address == ""
    assert cli.main([*flags, "--temporal-address", "temporal.test:443"]) == 0
    assert installed[-1].temporal_address == "temporal.test:443"


def test_settings_saved_with_or_without_a_temporal_address_load(
    tmp_path: Path, bin_dir: Path
) -> None:
    paths = workstation.RunnerPaths(root=tmp_path, name="acme")
    paths.create()
    _settings(paths, str(bin_dir)).save(paths)
    loaded = workstation.WorkstationSettings.load(paths)
    assert workstation.run_environment(paths, loaded)["TEMPORAL_ADDRESS"] == (
        "temporal-grpc.test:443"
    )

    saved = json.loads(paths.settings_file.read_text())
    del saved["temporal_address"]
    paths.settings_file.write_text(json.dumps(saved))
    assert workstation.WorkstationSettings.load(paths).temporal_address == ""


# ------------------------------------------------------------------ the CLIs


def test_the_runner_locates_both_clis_on_path_and_reports_their_floors(bin_dir: Path) -> None:
    found = workstation.locate_clis(str(bin_dir))
    assert found == {"codex": str(bin_dir / "codex"), "claude": str(bin_dir / "claude")}

    # Keyed by the Profile's `cli_kind`: what the registry serves and routing matches on.
    versions = {cli.cli_kind: cli for cli in workstation.cli_versions(found)}
    assert versions["codex_cli"].version == "0.141.2"
    assert versions["codex_cli"].meets_floor is True
    assert versions["claude_code"].version == "2.1.279"
    assert versions["claude_code"].meets_floor is False


def test_no_cli_on_path_refuses_the_start_and_one_is_enough(
    tmp_path: Path, bin_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (bin_dir / "claude").unlink()
    paths = workstation.RunnerPaths(root=tmp_path / "state", name="acme")
    paths.create()
    # One CLI serves that one runtime (local-agents 01); no per-Profile choice to refuse on.
    assert workstation.require_any_cli(str(bin_dir)) == {"codex": str(bin_dir / "codex")}

    (bin_dir / "codex").unlink()
    _settings(paths, str(bin_dir)).save(paths)

    assert workstation.run_process(paths, login_agent=True) == 1
    assert "refusing to start (cli_missing): none of" in capsys.readouterr().out


def test_settings_saved_with_the_retired_cli_kind_still_load(tmp_path: Path, bin_dir: Path) -> None:
    paths = workstation.RunnerPaths(root=tmp_path / "state", name="acme")
    paths.create()
    saved = _settings(paths, str(bin_dir)).model_dump(mode="json") | {"cli_kind": "codex_cli"}
    paths.settings_file.write_text(json.dumps(saved), encoding="utf-8")

    assert workstation.WorkstationSettings.load(paths).path == str(bin_dir)


@pytest.fixture
def opened() -> Iterator[list[str]]:
    """Every path this process opens while the fixture is live (``sys.addaudithook``)."""

    seen: list[str] = []
    live = [True]

    def hook(event: str, args: tuple[Any, ...]) -> None:
        if live[0] and event == "open" and args and isinstance(args[0], str | bytes | Path):
            seen.append(os.fsdecode(args[0]))

    sys.addaudithook(hook)
    yield seen
    live[0] = False  # an audit hook cannot be removed, only disarmed


@pytest.mark.asyncio
async def test_the_runner_never_opens_a_file_under_any_harness_root(
    tmp_path: Path,
    bin_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    opened: list[str],
) -> None:
    home = tmp_path / "home"
    paths = workstation.RunnerPaths(root=tmp_path / "state", name="acme")
    contract = str(uuid4())
    # Every harness root the box holds: the user's own CLI logins and the per-Contract
    # roots the funders created (issue 30's layout), each with a credential-shaped file.
    harness_roots = [
        home / ".codex",
        home / ".claude",
        paths.workspaces / contract / "harness" / "codex",
        paths.workspaces / contract / "harness" / "claude",
    ]
    for root in harness_roots:
        root.mkdir(parents=True)
        (root / "auth.json").write_text('{"token": "never-read"}')
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(harness_roots[0]))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(harness_roots[1]))
    plane = FakeControlPlane(host_party="user")
    opened.clear()  # the setup above wrote them; from here on it is the Runner

    async with httpx.AsyncClient(transport=plane.transport()) as http:
        await workstation.install(
            paths,
            _settings(paths, str(bin_dir)),
            registration_token="registration-token-of-sixteen-plus",
            platform="linux",
            home=home,
            run=Commands(),
            http_client=http,
        )
    store = FileCredentialStore(paths.credentials)
    store.put("deploy_key", "value")
    attestation = workstation.collect_attestation(
        channel=InstallChannel.SYSTEMD_USER,
        session=SessionKind.LOGIN_AGENT,
        store=store.kind,
        path=str(bin_dir),
    )
    workstation.status_lines(paths)
    await _run_once(paths, plane, store=store, attestation=attestation, monkeypatch=monkeypatch)

    assert opened, "the audit hook saw nothing -- it is not watching"
    touched = [path for path in opened for root in harness_roots if Path(path).is_relative_to(root)]
    assert touched == []


async def _run_once(
    paths: workstation.RunnerPaths,
    plane: FakeControlPlane,
    *,
    store: Any,
    attestation: HostAttestation,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``service.run`` as ``run --name`` wires it, until the first heartbeat lands."""

    settings = workstation.WorkstationSettings.load(paths)
    for name, value in workstation.run_environment(paths, settings).items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("AGENTIC_RUNNER_SOCKET_DIR", str(paths.state_dir / "s"))
    monkeypatch.delenv("INTERNAL_FASTAPI_BASE_URL", raising=False)
    stop = asyncio.Event()

    class Worker:
        def __init__(self, *_: Any, **__: Any) -> None:
            self._done = asyncio.Event()

        async def run(self) -> None:
            await self._done.wait()

        async def shutdown(self) -> None:
            self._done.set()

    async def connect(*_: Any, **__: Any) -> Any:
        return type("Client", (), {"api_key": None})()

    async def stop_once_heard() -> None:
        await plane.wait_for(lambda: bool(plane.heartbeats))
        stop.set()

    stopper = asyncio.create_task(stop_once_heard())
    async with httpx.AsyncClient(transport=plane.transport()) as http:
        code = await service.run(
            http_client=http,
            connect=connect,
            worker_factory=Worker,
            can_change_uid=False,
            stop=stop,
            heartbeat_interval=0.05,
            host_store=store,
            attestation=attestation,
        )
    await stopper
    assert code == 0


# ------------------------------------------------------------------ storage


class FakeKeychain:
    """``security`` as the login keychain answers it -- in memory, never on disk."""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], str] = {}
        self.argv_seen: list[list[str]] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.argv_seen.append(argv)
        if argv[1] == "-i":
            words = re.findall(r'"((?:[^"\\]|\\.)*)"', kwargs["input"])
            service, account, value = (w.replace('\\"', '"').replace("\\\\", "\\") for w in words)
            self.items[(service, account)] = value
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[1] == "find-generic-password":
            value = self.items.get((argv[3], argv[5]))
            return subprocess.CompletedProcess(argv, 0 if value else 44, (value or "") + "\n", "")
        return subprocess.CompletedProcess(argv, 0, "", "")


def test_a_value_stored_through_the_os_store_reads_back_and_is_absent_from_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("agentic_runner.host_store.shutil.which", lambda name: f"/usr/bin/{name}")
    keychain = FakeKeychain()
    paths = workstation.RunnerPaths(root=tmp_path / "state", name="acme")
    paths.create()
    secret = 'sk-live-"quoted" value'

    store = open_workstation_store(
        "acme", fallback=paths.credentials, platform="darwin", run=keychain
    )
    assert store.kind is StoreKind.KEYCHAIN
    store.put("deploy_key", secret)

    assert store.get("deploy_key") == secret
    # Read by the Runner through the same resolver a Directive uses.
    resolved = service.build_credential_resolver(
        service.load_config(environ={}), host_store=store
    ).resolve(contract_id=None, manifest=["deploy_key"])
    assert resolved.for_verb_seam("deploy_key") == secret
    # Nowhere on disk, and never in argv (every process on the box can read argv).
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert secret not in path.read_text(errors="ignore")
    assert all(secret not in " ".join(argv) for argv in keychain.argv_seen)


@pytest.mark.skipif(sys.platform != "darwin", reason="the real Keychain is macOS-only")
def test_the_real_keychain_round_trips_a_value_off_disk(tmp_path: Path) -> None:
    keychain = tmp_path / "test.keychain-db"
    subprocess.run(["security", "create-keychain", "-p", "pw", str(keychain)], check=True)
    try:
        subprocess.run(["security", "unlock-keychain", "-p", "pw", str(keychain)], check=True)
        store = KeychainStore("agentic-runner.test", keychain=keychain)
        assert store.available()
        store.put("deploy_key", 'va"l\\ue')
        assert store.get("deploy_key") == 'va"l\\ue'
        assert store.get("absent") is None
    finally:
        subprocess.run(["security", "delete-keychain", str(keychain)], check=False)


def test_with_no_os_store_reachable_the_fallback_file_is_created_0600(tmp_path: Path) -> None:
    def unreachable(argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 1, "", "Cannot autolaunch D-Bus")

    paths = workstation.RunnerPaths(root=tmp_path / "state", name="acme")
    store = open_workstation_store(
        "acme", fallback=paths.credentials, platform="linux", run=unreachable
    )
    assert store.kind is StoreKind.FILE

    store.put("deploy_key", "value")

    assert (paths.credentials / "deploy_key").stat().st_mode & 0o777 == 0o600
    assert paths.credentials.stat().st_mode & 0o777 == 0o700
    assert store.get("deploy_key") == "value"
    with pytest.raises(ValueError, match="single name"):
        store.put("../elsewhere", "value")


# ------------------------------------------------------------------ attestation


def test_the_heartbeat_attestation_carries_the_facts_and_nothing_content_shaped(
    bin_dir: Path,
) -> None:
    attestation = workstation.collect_attestation(
        channel=InstallChannel.HOMEBREW,
        session=SessionKind.LOGIN_AGENT,
        store=StoreKind.KEYCHAIN,
        path=str(bin_dir),
    )
    fields = set(HeartbeatEnvelope.model_fields)

    # 23 item 5's list, field by field.
    assert {"attestation", "harnesses", "last_sleep_at", "last_wake_at"} <= fields
    assert {"clock_skew_seconds", "slots", "lifecycle"} <= fields
    assert set(HostAttestation.model_fields) == {
        "build_id",
        "install_channel",
        "os",
        "session_kind",
        "store_kind",
        "clis",
    }
    assert attestation.build_id == build_id()
    assert {cli.cli_kind for cli in attestation.clis} == {"codex_cli", "claude_code"}
    dumped = attestation.model_dump_json()
    assert str(bin_dir) not in dumped  # a path carries the user's home directory

    base = attestation.model_dump()
    for content_shaped in (
        {"os": "darwin 25.0.0 on example-laptop.local"},
        {"os": "/Users/someone"},
        {"build_id": "not a digest"},
        {
            "clis": [
                {
                    "cli_kind": "codex_cli",
                    "version": "/opt/homebrew/bin/codex",
                    "meets_floor": True,
                }
            ]
        },
        {"hostname": "laptop"},
        {"cli_path": "/Users/someone/.local/bin/claude"},
    ):
        with pytest.raises(ValidationError):
            HostAttestation.model_validate({**base, **content_shaped})


def test_the_build_id_is_a_full_digest_of_the_package_sources(tmp_path: Path) -> None:
    package = tmp_path / "agentic_runner"
    shutil.copytree(
        Path(agentic_runner.__file__).parent,
        package,
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    original = build_id(package)

    assert re.fullmatch(r"[0-9a-f]{64}", original)
    assert original == build_id()  # the copy is this build

    # Bytecode is not the build: a wheel, the image and a `uv tool install` compile their own.
    (package / "stray.pyc").write_bytes(b"compiled")
    assert build_id(package) == original

    service_module = package / "service.py"
    service_module.write_text(service_module.read_text() + "\n# changed\n")
    assert build_id(package) != original


@pytest.mark.asyncio
async def test_a_wake_is_attested_with_its_sleep_and_queued_as_a_lifecycle_fact(
    tmp_path: Path,
) -> None:
    class Registration:
        server_date = datetime(2026, 9, 23, 10, 0, tzinfo=UTC)

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

    from agentic_runner.credentials import CredentialResolver, EmptyCredentialStore
    from agentic_runner.llm_proxy import CeilingStore, LlmProxy, SlotStore, UsageOutbox
    from agentic_runner.registration import RunnerState
    from agentic_runner.sealed_box import RecipientKeyStore, SealedCredentialStream

    wall = [datetime(2026, 9, 23, 10, 0, 5, tzinfo=UTC)]
    monotonic = [100.0]
    lifecycle = LifecycleOutbox(tmp_path)
    registration = Registration()
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
        proxy=LlmProxy(slots=SlotStore(), ceilings=CeilingStore(), outbox=UsageOutbox()),
        sealed=SealedCredentialStream(RecipientKeyStore(tmp_path)),
        credentials=CredentialResolver(store=EmptyCredentialStore()),
        load=service._LoadInterceptor(),
        clock=lambda: wall[0],
        monotonic=lambda: monotonic[0],
        lifecycle=lifecycle,
        heartbeat_stamp=tmp_path / "last-heartbeat",
    )

    await stream.exchange([])
    # Thirty seconds awake, then the lid: eight hours of wall clock the monotonic clock
    # never saw.
    monotonic[0] += 30
    wall[0] += timedelta(hours=8, seconds=30)
    await stream.exchange([])

    first, second = registration.sent
    assert first.last_wake_at is None
    assert first.clock_skew_seconds == 5.0
    assert second.last_sleep_at == datetime(2026, 9, 23, 10, 0, 35, tzinfo=UTC)
    assert second.last_wake_at == wall[0]
    [wake] = second.lifecycle
    assert wake.kind is LifecycleKind.WAKE and wake.slept_at == second.last_sleep_at
    # Acknowledged, so the next beat does not carry it again.
    assert lifecycle.pending() == []
    assert (tmp_path / "last-heartbeat").exists()


def test_an_organisation_name_is_one_safe_segment() -> None:
    assert workstation.validate_name("acme-2") == "acme-2"
    for bad in ("", "Acme", "../x", "a" * 33, "a b", "a.b"):
        with pytest.raises(ValueError):
            workstation.validate_name(bad)


def test_the_state_root_follows_each_os_per_user_convention() -> None:
    home = {"HOME": "/home/dev"}
    assert workstation.default_root(platform="linux", environ=home) == Path(
        "/home/dev/.local/state/agentic-runner"
    )
    assert workstation.default_root(platform="darwin", environ=home) == Path(
        "/home/dev/Library/Application Support/agentic-runner"
    )
    assert workstation.default_root(
        platform="win32", environ={**home, "LOCALAPPDATA": "C:/Users/dev/AppData/Local"}
    ) == Path("C:/Users/dev/AppData/Local/agentic-runner")


@pytest.mark.asyncio
async def test_a_pool_runner_install_keeps_the_pool_key_private_and_out_of_the_agent(
    tmp_path: Path, bin_dir: Path
) -> None:
    """Spec: Runners rev 3: every start of the Runner needs the Pool Key, so the install
    keeps it -- 0600 beside the identity, never in the login agent's definition."""

    home = tmp_path / "home"
    paths = workstation.RunnerPaths(root=tmp_path / "state", name="acme")
    pool_key = generate_recipient_key()

    async with httpx.AsyncClient(transport=FakeControlPlane().transport()) as http:
        await workstation.install(
            paths,
            _settings(paths, str(bin_dir)),
            registration_token="registration-token-of-sixteen-plus",
            pool_key=pool_key.private_key,
            platform="darwin",
            home=home,
            run=Commands(),
            http_client=http,
        )

    held = load_pool_key(paths.state_dir, {})
    assert held is not None and held.fingerprint == pool_key.fingerprint
    plist = home / "Library/LaunchAgents/agentic-runner.acme.plist"
    assert pool_key.private_key not in plist.read_text()
    assert POOL_KEY_ENV not in workstation.run_environment(
        paths, workstation.WorkstationSettings.load(paths)
    )


@pytest.mark.parametrize("given", ["flag", "environment", "neither", "not_a_key"])
def test_install_takes_the_pool_key_from_its_prompt_or_its_install_line(
    tmp_path: Path, bin_dir: Path, monkeypatch: pytest.MonkeyPatch, given: str
) -> None:
    pool_key = generate_recipient_key().private_key
    installed: list[str | None] = []
    prompts: list[str] = []

    async def install(*_: Any, pool_key: str | None = None, **__: Any) -> list[str]:
        if pool_key == "not-a-key":
            raise PoolKeyError("not a Pool Key")
        installed.append(pool_key)
        return []

    def prompt(text: str) -> str:
        prompts.append(text)
        return pool_key

    monkeypatch.setenv(cli.REGISTRATION_TOKEN_ENV, "registration-token-of-sixteen-plus")
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setattr(cli.workstation, "install", install)
    monkeypatch.setattr(cli.getpass, "getpass", prompt)
    if given in {"environment", "not_a_key"}:
        monkeypatch.setenv(POOL_KEY_ENV, pool_key if given == "environment" else "not-a-key")
    flags = ["install", "acme", "--control-plane", CONTROL_PLANE, "--root", str(tmp_path)]

    exit_code = cli.main([*flags, "--pool-key"] if given == "flag" else flags)

    if given == "not_a_key":
        assert (exit_code, installed) == (1, [])
        return
    assert exit_code == 0
    assert installed == [None if given == "neither" else pool_key]
    assert len(prompts) == (1 if given == "flag" else 0), "never a flag value"
    assert POOL_KEY_ENV not in os.environ
