"""The workstation Runner: one login agent per Organisation (PRD issue 47, map ticket 23).

``agentic-runner install | start | stop | status <org>``. Everything a CI-runner
precedent does on a developer machine and nothing a daemon would need:

* **One process per Organisation** (23 item 3), each with its own state directory,
  identity, namespace, Runner Token, hooks, credential store and Workspaces under
  ``<root>/<org>/``. A contractor with two clients runs two. There is no supervisor over
  N Organisations -- that is the "one Runner straddling Organisations" 12 A6 rejected.
* **A login agent, never a system daemon** (23 item 1): a LaunchAgent in
  ``~/Library/LaunchAgents`` on macOS, a systemd *user* unit (with linger) on Linux, a
  per-user logon task on Windows. No ``sudo`` anywhere: the login keychain is unreachable
  from a daemon (Apple TN3137), and a Runner that needed root would be the wrong shape.
  No self-update in Release 1 -- the control plane's version floor (issue 41) is the lever.
* **Native process, CLIs found not bundled** (23 item 2): ``codex`` and ``claude`` are
  looked up on the ``PATH`` captured at install, their versions reported on the heartbeat,
  and a missing CLI the Profile names refuses the start. The Runner never opens a file
  under a harness root -- the per-Contract ``CODEX_HOME`` / ``CLAUDE_CONFIG_DIR`` belongs
  to the login the funder created there (issues 30, 31).
* **``isolation: none``** (17 A2): no root and no uid switch, so the control plane routes
  at most one Contract at a time here (issue 42), which one Organisation per process and
  one active Contract per (Organisation, User Profile) pair already implied.
"""

from __future__ import annotations

import asyncio
import os
import platform as platform_module
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final
from xml.sax.saxutils import escape

import httpx
from pydantic import BaseModel, ConfigDict

from agentic_runner import service
from agentic_runner.build import build_id
from agentic_runner.host_store import open_workstation_store
from agentic_runner.lifecycle import LifecycleOutbox
from agentic_runner.registration import load_state
from agentic_runner.service import HEARTBEAT_STAMP_NAME, PIDFILE_NAME
from agentic_runner_contracts.runner_registration import (
    CliVersion,
    EgressPosture,
    HostAttestation,
    InstallChannel,
    IsolationMode,
    LifecycleKind,
    SessionKind,
    StoreKind,
)

__all__ = [
    "CLI_FLOORS",
    "CLI_NAMES",
    "ROOT_ENV",
    "CliMissingError",
    "OrgPaths",
    "ServiceDefinition",
    "WorkstationSettings",
    "cli_versions",
    "collect_attestation",
    "default_root",
    "install",
    "install_channel",
    "locate_clis",
    "require_any_cli",
    "run_environment",
    "run_process",
    "service_definition",
    "start",
    "status_lines",
    "stop",
    "validate_org",
]

ROOT_ENV: Final = "AGENTIC_RUNNER_WORKSTATION_ROOT"

# What the Profile's `cli_kind` runs, by executable name on PATH.
CLI_NAMES: Final[Mapping[str, str]] = {"codex_cli": "codex", "claude_code": "claude"}
# The versions the Runner image is built and tested against (Dockerfile.runner's
# CODEX_CLI_VERSION / CLAUDE_CODE_VERSION; a test holds the two together). Below the
# floor is *reported*, not refused: the user's own CLI is the user's to upgrade.
CLI_FLOORS: Final[Mapping[str, str]] = {"codex": "0.141.0", "claude": "2.1.280"}

_ORG = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
_VERSION = re.compile(r"(\d+\.\d+\.\d+[0-9A-Za-z.+-]{0,40})")
_LABEL_PREFIX: Final = "agentic-runner"

Run = Callable[..., subprocess.CompletedProcess[str]]


class CliMissingError(RuntimeError):
    pass


def validate_org(org: str) -> str:
    """One path segment, one launchd label, one systemd instance name -- all at once."""

    if not _ORG.fullmatch(org):
        raise ValueError(
            f"Organisation name {org!r} must be 1-32 of lowercase letters, digits and '-'"
        )
    return org


def default_root(*, platform: str = sys.platform, environ: Mapping[str, str] = os.environ) -> Path:
    """Where every Organisation's directory lives, per the OS's per-user convention."""

    if environ.get(ROOT_ENV, "").strip():
        return Path(environ[ROOT_ENV])
    home = Path(environ.get("HOME") or Path.home())
    if platform == "darwin":
        return home / "Library" / "Application Support" / "agentic-runner"
    if platform == "win32":
        return Path(environ.get("LOCALAPPDATA") or home / "AppData" / "Local") / "agentic-runner"
    state_home = environ.get("XDG_STATE_HOME", "").strip()
    return (Path(state_home) if state_home else home / ".local" / "state") / "agentic-runner"


@dataclass(frozen=True)
class OrgPaths:
    """``<root>/<org>/`` and everything the process keeps under it."""

    root: Path
    org: str

    @property
    def state_dir(self) -> Path:
        return self.root / self.org

    @property
    def hooks(self) -> Path:
        # Issue 45's path, per map ticket 26: `<state_dir>/<org>/hooks/`.
        return self.state_dir / "hooks"

    @property
    def workspaces(self) -> Path:
        # Issue 30's `{contract_id}/{work_record_id}` layout sits under here, and the
        # per-Contract harness roots with it (`{contract_id}/harness/{runtime_kind}`).
        return self.state_dir / "workspaces"

    @property
    def contract_state(self) -> Path:
        return self.state_dir / "contracts"

    @property
    def credentials(self) -> Path:
        return self.state_dir / "credentials"

    @property
    def settings_file(self) -> Path:
        return self.state_dir / "workstation.json"

    @property
    def pidfile(self) -> Path:
        return self.state_dir / PIDFILE_NAME

    @property
    def heartbeat_stamp(self) -> Path:
        return self.state_dir / HEARTBEAT_STAMP_NAME

    @property
    def log(self) -> Path:
        return self.state_dir / "runner.log"

    @property
    def socket_dir(self) -> Path:
        # `sun_path` is ~104 bytes and "~/Library/Application Support/..." is most of
        # that already, so the per-attempt sockets get a short *per-user* root: the
        # session's runtime dir, or macOS's per-user `$TMPDIR` -- never a shared `/tmp`
        # another user could pre-create the directory in.
        runtime = os.environ.get("XDG_RUNTIME_DIR", "").strip() or (
            tempfile.gettempdir() if sys.platform == "darwin" else ""
        )
        if runtime:
            return Path(runtime) / f"agentic-runner-{self.org}"
        return self.state_dir / "sockets"

    def create(self) -> None:
        for directory in (self.state_dir, self.hooks, self.workspaces, self.contract_state):
            directory.mkdir(parents=True, exist_ok=True)
        # The identity's private key and the fallback credential files are in here.
        self.state_dir.chmod(0o700)


class WorkstationSettings(BaseModel):
    """What ``install`` was told, so ``run`` and ``status`` need no flags."""

    # `ignore`, not `forbid`: only `save` writes this file, and an install made before
    # local-agents 01 retired the per-process `cli_kind` still has to `run` and `status`.
    model_config = ConfigDict(extra="ignore", frozen=True)

    org: str
    control_plane_url: str
    temporal_address: str
    # `temporal-grpc.<zone>` over TLS with the Runner Token (15's note on 23): no VPN, no
    # client certificate. Plaintext exists for a local dev server only.
    temporal_tls: bool = True
    # The PATH `install` ran under: a LaunchAgent starts with `/usr/bin:/bin:...` and
    # would not find a Homebrew or `~/.local/bin` CLI without it.
    path: str
    install_channel: InstallChannel

    def save(self, paths: OrgPaths) -> None:
        paths.settings_file.write_text(self.model_dump_json(indent=2), encoding="utf-8")

    @classmethod
    def load(cls, paths: OrgPaths) -> WorkstationSettings:
        if not paths.settings_file.is_file():
            raise FileNotFoundError(
                f"no Runner is installed for {paths.org!r} under {paths.root}; "
                f"run `agentic-runner install {paths.org}` first"
            )
        return cls.model_validate_json(paths.settings_file.read_text(encoding="utf-8"))


def install_channel(
    platform: str = sys.platform, *, executable: str = sys.executable
) -> InstallChannel:
    if platform == "darwin":
        return InstallChannel.HOMEBREW if "/Cellar/" in executable else InstallChannel.MANUAL
    if platform == "win32":
        return InstallChannel.WINDOWS_USER
    return InstallChannel.SYSTEMD_USER


def run_environment(paths: OrgPaths, settings: WorkstationSettings) -> dict[str, str]:
    """The environment ``agentic-runner run --org`` gives ``service.run``.

    Set here rather than in the service definition so one definition format -- a plist,
    a unit, a task -- stays a one-liner, and the three OSes cannot drift apart.
    """

    return {
        "PATH": settings.path,
        "AGENTIC_RUNNER_STATE_DIR": str(paths.state_dir),
        "AGENTIC_RUNNER_WORKSPACE_ROOT": str(paths.workspaces),
        "AGENTIC_RUNNER_HOOKS_PATH": str(paths.hooks),
        "AGENTIC_RUNNER_SOCKET_DIR": str(paths.socket_dir),
        "AGENTIC_RUNNER_ISOLATION": "none",
        # A laptop has nothing beneath the attempt's egress proxy to stop a raw socket,
        # so it reports the Profile's allow-list as unenforced (PRD issue 58).
        service.EGRESS_POSTURE_ENV: EgressPosture.UNENFORCED.value,
        "AGENTIC_CONTROL_PLANE_URL": settings.control_plane_url,
        "AGENTIC_TEMPORAL_TLS": "1" if settings.temporal_tls else "0",
        "TEMPORAL_ADDRESS": settings.temporal_address,
        "WORKSPACE_ROOT": str(paths.workspaces),
        "WORKER_STATE_DIR": str(paths.contract_state),
        # Ephemeral: one process per Organisation shares the host, so a fixed readiness
        # port inherited from a pod-style environment would collide on the second one.
        service.READINESS_PORT_ENV: "0",
    }


# ------------------------------------------------------------------ the CLIs


def locate_clis(path: str) -> dict[str, str]:
    """``{"codex": "/opt/homebrew/bin/codex", ...}`` for each CLI found on ``path``."""

    return {
        name: found
        for name in CLI_NAMES.values()
        if (found := shutil.which(name, path=path)) is not None
    }


def require_any_cli(path: str) -> dict[str, str]:
    """The CLIs on ``path``, or a refusal that says what to install.

    One is enough: the Runner serves every Agent Runtime it finds and is routed only the
    Work Records whose ``cli_kind`` it reports (local-agents 01), so a missing CLI costs
    that runtime, never the start.
    """

    found = locate_clis(path)
    if not found:
        raise CliMissingError(
            f"none of {sorted(CLI_NAMES.values())} is on PATH ({path}). Install one and "
            "log in, then run `agentic-runner install` again so the new PATH is captured"
        )
    return found


def _version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", version)[:3])


def cli_versions(found: Mapping[str, str], *, run: Run = subprocess.run) -> list[CliVersion]:
    """Each CLI's ``--version``, the only thing the Runner asks a CLI about itself.

    Keyed by the Profile's ``cli_kind`` (``codex_cli``), not the executable: it is what
    the Runner's registry is built from and what routing matches a Work Record on.
    """

    versions: list[CliVersion] = []
    for cli_kind, name in sorted(CLI_NAMES.items()):
        executable = found.get(name)
        if executable is None:
            continue
        try:
            answer = run(
                [executable, "--version"], capture_output=True, text=True, timeout=10, check=False
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        match = _VERSION.search(answer.stdout or answer.stderr)
        if match is None:
            continue
        version = match.group(1)
        versions.append(
            CliVersion(
                cli_kind=cli_kind,
                version=version,
                meets_floor=_version_tuple(version) >= _version_tuple(CLI_FLOORS[name]),
            )
        )
    return versions


# ------------------------------------------------------------------ attestation


def os_release() -> str:
    system = re.sub(r"[^a-z0-9_]", "", platform_module.system().lower())[:16] or "unknown"
    release = re.sub(r"[^0-9A-Za-z._+-]", "", platform_module.release())[:48]
    return f"{system} {release}" if release else system


def collect_attestation(
    *,
    channel: InstallChannel,
    session: SessionKind,
    store: StoreKind,
    path: str,
    run: Run = subprocess.run,
) -> HostAttestation:
    """Collected once at start-up: none of it changes while the process lives."""

    return HostAttestation(
        build_id=build_id(),
        install_channel=channel,
        os=os_release(),
        session_kind=session,
        store_kind=store,
        clis=cli_versions(locate_clis(path), run=run),
    )


# ------------------------------------------------------------------ service definitions


@dataclass(frozen=True)
class ServiceDefinition:
    """One OS's per-user service: the file it installs and the commands that drive it."""

    path: Path
    content: bytes
    install: list[list[str]]
    start: list[list[str]]
    stop: list[list[str]]


def runner_command(org: str, root: Path) -> list[str]:
    # The console script as found, not resolved: Homebrew's `bin/agentic-runner` is a
    # symlink into a versioned Cellar path that the next `brew upgrade` deletes.
    script = shutil.which("agentic-runner")
    prefix = [script] if script else [sys.executable, "-m", "agentic_runner.cli"]
    return [*prefix, "run", "--org", org, "--root", str(root), "--login-agent"]


def service_definition(
    paths: OrgPaths,
    settings: WorkstationSettings,
    *,
    platform: str = sys.platform,
    home: Path | None = None,
    user: str | None = None,
    uid: int | None = None,
    command: Sequence[str] | None = None,
) -> ServiceDefinition:
    home = home or Path.home()
    argv = list(command or runner_command(paths.org, paths.root))
    name = f"{_LABEL_PREFIX}.{paths.org}"
    if platform == "darwin":
        return _launch_agent(paths, settings, argv, name=name, home=home, uid=uid)
    if platform == "win32":
        return _logon_task(paths, argv, name=name, user=user)
    return _systemd_user_unit(paths, settings, argv, home=home, user=user)


def _launch_agent(
    paths: OrgPaths,
    settings: WorkstationSettings,
    argv: list[str],
    *,
    name: str,
    home: Path,
    uid: int | None,
) -> ServiceDefinition:
    plist = home / "Library" / "LaunchAgents" / f"{name}.plist"
    domain = f"gui/{os.getuid() if uid is None else uid}"
    content = plistlib.dumps(
        {
            "Label": name,
            "ProgramArguments": argv,
            "EnvironmentVariables": {"PATH": settings.path},
            "RunAtLoad": True,
            # Restart a crash, not a clean `stop`: SIGTERM drains and exits 0.
            "KeepAlive": {"SuccessfulExit": False},
            "ProcessType": "Background",
            "StandardOutPath": str(paths.log),
            "StandardErrorPath": str(paths.log),
        }
    )
    return ServiceDefinition(
        path=plist,
        content=content,
        install=[],
        start=[["launchctl", "bootstrap", domain, str(plist)]],
        stop=[["launchctl", "bootout", f"{domain}/{name}"]],
    )


def _systemd_user_unit(
    paths: OrgPaths,
    settings: WorkstationSettings,
    argv: list[str],
    *,
    home: Path,
    user: str | None,
) -> ServiceDefinition:
    unit_name = f"{_LABEL_PREFIX}-{paths.org}.service"
    unit = home / ".config" / "systemd" / "user" / unit_name
    exec_start = " ".join(_systemd_quote(arg) for arg in argv)
    content = (
        "[Unit]\n"
        f"Description=Agentic OS Runner for {paths.org}\n"
        "After=network-online.target\n\n"
        "[Service]\n"
        f"ExecStart={exec_start}\n"
        f"Environment={_systemd_quote('PATH=' + settings.path)}\n"
        "Restart=on-failure\n"
        "RestartSec=10\n"
        # The drain waits out an in-flight Directive (issue 02); systemd's default 90 s
        # would SIGKILL it.
        "TimeoutStopSec=2100\n"
        "KillMode=mixed\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    ).encode()
    systemctl = ["systemctl", "--user"]
    return ServiceDefinition(
        path=unit,
        content=content,
        # Linger keeps a user's units running with nobody logged in -- a reboot of an
        # unattended workstation -- and needs no root for one's own user.
        install=[
            [*systemctl, "daemon-reload"],
            ["loginctl", "enable-linger", user or os.environ.get("USER", "")],
        ],
        start=[[*systemctl, "enable", "--now", unit_name]],
        stop=[[*systemctl, "disable", "--now", unit_name]],
    )


def _systemd_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _logon_task(
    paths: OrgPaths, argv: list[str], *, name: str, user: str | None
) -> ServiceDefinition:
    task_xml = paths.state_dir / f"{name}.task.xml"
    task_name = f"\\agentic-runner\\{paths.org}"
    who = user or os.environ.get("USERNAME", "")
    arguments = subprocess.list2cmdline(argv[1:])
    content = f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Agentic OS Runner for {escape(paths.org)}</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger><Enabled>true</Enabled><UserId>{escape(who)}</UserId></LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{escape(who)}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <RestartOnFailure><Interval>PT1M</Interval><Count>999</Count></RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec><Command>{escape(argv[0])}</Command><Arguments>{escape(arguments)}</Arguments></Exec>
  </Actions>
</Task>
""".encode("utf-16")
    return ServiceDefinition(
        path=task_xml,
        content=content,
        install=[["schtasks", "/Create", "/TN", task_name, "/XML", str(task_xml), "/F"]],
        start=[
            ["schtasks", "/Change", "/TN", task_name, "/ENABLE"],
            ["schtasks", "/Run", "/TN", task_name],
        ],
        stop=[
            ["schtasks", "/End", "/TN", task_name],
            ["schtasks", "/Change", "/TN", task_name, "/DISABLE"],
        ],
    )


def drive(commands: list[list[str]], *, run: Run = subprocess.run) -> None:
    for command in commands:
        done = run(command, capture_output=True, text=True, check=False)
        if done.returncode != 0:
            raise RuntimeError(
                f"`{' '.join(command)}` exited {done.returncode}: {done.stderr.strip()}"
            )


# ------------------------------------------------------------------ status


def process_id(paths: OrgPaths) -> int | None:
    """The running process's pid, if the pidfile names a live one."""

    try:
        pid = int(paths.pidfile.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        return pid
    return pid


def heartbeat_age(paths: OrgPaths, *, now: datetime | None = None) -> float | None:
    try:
        stamped = paths.heartbeat_stamp.stat().st_mtime
    except OSError:
        return None
    return max(0.0, (now or datetime.now(UTC)).timestamp() - stamped)


def status_lines(paths: OrgPaths, *, now: datetime | None = None) -> list[str]:
    """``status <org>``: the process, its identity and its heartbeat age."""

    pid = process_id(paths)
    state = load_state(paths.state_dir)
    age = heartbeat_age(paths, now=now)
    lines = [
        f"organisation   {paths.org} ({paths.state_dir})",
        f"process        {'running, pid ' + str(pid) if pid else 'not running'}",
        (
            f"identity       {state.runner_id} in {state.temporal_namespace} on {state.task_queue}"
            if state is not None
            else "identity       not registered"
        ),
        f"heartbeat      {'never' if age is None else f'{age:.0f}s ago'}",
    ]
    if pid and state is not None:
        lines.extend(next_step_lines(state.host_party))
    return lines


def next_step_lines(host_party: str) -> list[str]:
    """Where to go once the Runner is live (QTS-1314): the dogfood install ended at
    ``started`` and left the person with no idea what the console wanted next.

    Paths, not URLs: the Runner knows its control plane, not the console, and the two need
    not share a domain (QTS-1300) -- the person minted the token on that console anyway.
    """

    if host_party == "user":
        return [
            "next           open /me/runners in the console to see this Runner, then "
            "/me/work/new and pick a Contract that says it runs on a Runner you host"
        ]
    if host_party:
        return [
            "next           this Runner takes the work of Contracts whose Runner is hosted by "
            f"the {host_party}; it shows on the Organisation Console's Runners page"
        ]
    return []


# ------------------------------------------------------------------ the verbs


async def install(
    paths: OrgPaths,
    settings: WorkstationSettings,
    *,
    agent_token: str,
    platform: str = sys.platform,
    home: Path | None = None,
    start_service: bool = True,
    run: Run = subprocess.run,
    http_client: httpx.AsyncClient | None = None,
) -> list[str]:
    """Register this Organisation's process and install its login agent.

    Registration happens here, once, with the Agent Token this user minted on /me/runners
    or the Organisation's Admin issued them (12 A6, QTS-1304) -- pasted at the prompt,
    never stored: the identity bootstrap hands back is what every later start uses, and a
    lost state directory means a reinstall and a re-delivery of any sealed value (22 A4),
    by design.
    """

    require_any_cli(settings.path)
    paths.create()
    settings.save(paths)
    state, outcome = await service.register(
        state_dir=paths.state_dir,
        can_separate_uids=False,
        client=http_client,
        control_plane=settings.control_plane_url,
        agent_token=agent_token,
        isolation=IsolationMode.NONE,
    )
    LifecycleOutbox(paths.state_dir).add(LifecycleKind.INSTALL, datetime.now(UTC))
    definition = service_definition(paths, settings, platform=platform, home=home)
    definition.path.parent.mkdir(parents=True, exist_ok=True)
    definition.path.write_bytes(definition.content)
    lines = [outcome, f"login agent    {definition.path}"]
    if start_service:
        drive(definition.install + definition.start, run=run)
        lines.append(f"started        {state.runner_id}")
        lines.extend(next_step_lines(state.host_party))
    return lines


def start(paths: OrgPaths, **driver: Any) -> None:
    _drive_service(paths, "start", **driver)


def stop(paths: OrgPaths, **driver: Any) -> None:
    """Stop this Organisation's process; every other Organisation's keeps polling."""

    _drive_service(paths, "stop", **driver)


def _drive_service(
    paths: OrgPaths,
    verb: str,
    *,
    platform: str = sys.platform,
    home: Path | None = None,
    run: Run = subprocess.run,
) -> None:
    settings = WorkstationSettings.load(paths)
    definition = service_definition(paths, settings, platform=platform, home=home)
    drive(definition.start if verb == "start" else definition.stop, run=run)


def run_process(paths: OrgPaths, *, login_agent: bool) -> int:
    """``agentic-runner run --org``: what the login agent executes."""

    settings = WorkstationSettings.load(paths)
    try:
        require_any_cli(settings.path)
    except CliMissingError as error:
        print(f"refusing to start (cli_missing): {error}", flush=True)
        return 1
    for name, value in run_environment(paths, settings).items():
        os.environ[name] = value
    store = open_workstation_store(paths.org, fallback=paths.credentials)
    attestation = collect_attestation(
        channel=settings.install_channel,
        session=SessionKind.LOGIN_AGENT if login_agent else SessionKind.INTERACTIVE,
        store=store.kind,
        path=settings.path,
    )
    return asyncio.run(service.run(host_store=store, attestation=attestation))
