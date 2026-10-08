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
from uuid import uuid4

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
from agentic_runner.testing import FakeControlPlane
from agentic_runner_contracts import __version__ as contracts_version
from agentic_runner_contracts.runner_registration import (
    FloorState,
    HeartbeatAck,
    HeartbeatEnvelope,
    HostAttestation,
    InstallChannel,
    LifecycleKind,
    SessionKind,
    StoreKind,
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


def _settings(paths: workstation.OrgPaths, path: str, **overrides: Any) -> Any:
    return workstation.WorkstationSettings(
        **{
            "org": paths.org,
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
    return definition.install + definition.start + definition.stop


@pytest.mark.parametrize("platform", ["darwin", "linux", "win32"])
def test_every_service_definition_is_user_level(tmp_path: Path, platform: str) -> None:
    home = tmp_path / "home"
    paths = workstation.OrgPaths(root=home / "state", org="acme")
    definition = workstation.service_definition(
        paths,
        _settings(paths, "/usr/bin:/bin"),
        platform=platform,
        home=home,
        user="dev",
        uid=501,
        command=["/opt/homebrew/bin/agentic-runner", "run", "--org", "acme"],
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
        assert agent["ProgramArguments"][1:4] == ["run", "--org", "acme"]
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


def test_the_cli_floors_are_the_versions_the_runner_image_pins() -> None:
    dockerfile = (REPO / "Dockerfile.runner").read_text()

    assert f"CODEX_CLI_VERSION={workstation.CLI_FLOORS['codex']}" in dockerfile
    assert f"CLAUDE_CODE_VERSION={workstation.CLI_FLOORS['claude']}" in dockerfile


@pytest.mark.asyncio
async def test_install_registers_writes_the_launch_agent_and_status_reports_it(
    tmp_path: Path, bin_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    paths = workstation.OrgPaths(root=tmp_path / "state", org="acme")
    plane = FakeControlPlane(host_party="user")
    commands = Commands()

    async with httpx.AsyncClient(transport=plane.transport()) as http:
        lines = await workstation.install(
            paths,
            _settings(paths, str(bin_dir)),
            agent_token="agent-token-of-sixteen-plus",
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

    workstation.stop(paths, platform="darwin", home=home, run=commands)
    assert commands.ran[-1] == ["launchctl", "bootout", f"gui/{os.getuid()}/agentic-runner.acme"]


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
    paths = workstation.OrgPaths(root=tmp_path / "state", org="acme")
    paths.create()
    # One CLI serves that one runtime (local-agents 01); no per-Profile choice to refuse on.
    assert workstation.require_any_cli(str(bin_dir)) == {"codex": str(bin_dir / "codex")}

    (bin_dir / "codex").unlink()
    _settings(paths, str(bin_dir)).save(paths)

    assert workstation.run_process(paths, login_agent=True) == 1
    assert "refusing to start (cli_missing): none of" in capsys.readouterr().out


def test_settings_saved_with_the_retired_cli_kind_still_load(tmp_path: Path, bin_dir: Path) -> None:
    paths = workstation.OrgPaths(root=tmp_path / "state", org="acme")
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
    paths = workstation.OrgPaths(root=tmp_path / "state", org="acme")
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
            agent_token="agent-token-of-sixteen-plus",
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
    paths: workstation.OrgPaths,
    plane: FakeControlPlane,
    *,
    store: Any,
    attestation: HostAttestation,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``service.run`` as ``run --org`` wires it, until the first heartbeat lands."""

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
    paths = workstation.OrgPaths(root=tmp_path / "state", org="acme")
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

    paths = workstation.OrgPaths(root=tmp_path / "state", org="acme")
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
    assert workstation.validate_org("acme-2") == "acme-2"
    for bad in ("", "Acme", "../x", "a" * 33, "a b", "a.b"):
        with pytest.raises(ValueError):
            workstation.validate_org(bad)


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
