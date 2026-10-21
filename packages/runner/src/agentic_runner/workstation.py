"""The workstation Runner: one login agent per Runner (PRD issue 47, map ticket 23).

``agentic-runner install | start | stop | restart | status <name>`` and ``list``. Everything
a CI-runner precedent does on a developer machine and nothing a daemon would need:

* **One process per Runner** (23 item 3), each with its own state directory, identity,
  namespace, Runner Token, hooks, credential store and Workspaces under ``<root>/<name>/``.
  A contractor with two clients runs two. There is no supervisor over N Runners -- that
  is the "one Runner straddling Organisations" 12 A6 rejected.
* **The name is the Runner's** (QTS-1773): the console lists the Runner under the name it
  was installed as, and every local command takes it. It was an Organisation label before,
  so the name a person chose on /me/runners read as an Organisation; an install made then
  keeps its directory, login agent and keychain entries, all keyed by the same string.
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
  at most one Contract at a time here (issue 42), which one Contract per personal Runner
  already implied.
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
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal
from xml.sax.saxutils import escape

import httpx
from pydantic import BaseModel, ConfigDict

from agentic_runner import __version__ as runner_version
from agentic_runner import service
from agentic_runner.build import build_id
from agentic_runner.cli_floor import CLI_FLOORS, CLI_NAMES, meets_floor, parse_cli_version
from agentic_runner.config import CONFIG_FILE_ENV
from agentic_runner.host_store import open_workstation_store
from agentic_runner.lifecycle import LifecycleOutbox
from agentic_runner.llm_contracts import load_llm_contracts
from agentic_runner.organisation_name import load_organisation_name
from agentic_runner.pool_key import parse_pool_key, save_pool_key
from agentic_runner.private_state import UnsafeStateError
from agentic_runner.registration import load_state
from agentic_runner.service import HEARTBEAT_STAMP_NAME, PIDFILE_NAME
from agentic_runner.workers.acp_runtime import ACP_BRIDGES
from agentic_runner_contracts.public_metadata import NAMESPACE_PREFIX
from agentic_runner_contracts.runner_registration import (
    RUNNER_NAME_PATTERN,
    CliVersion,
    ContractLlmTerms,
    EgressPosture,
    HostAttestation,
    InstallChannel,
    InstallTarget,
    IsolationMode,
    LifecycleKind,
    RunnerInstall,
    SessionKind,
    StoreKind,
)

__all__ = [
    "CLI_FLOORS",
    "CLI_NAMES",
    "DRAIN_TIMEOUT_SECONDS",
    "ROOT_ENV",
    "CliMissingError",
    "RunnerPaths",
    "ServiceDefinition",
    "WorkstationSettings",
    "cli_versions",
    "collect_attestation",
    "default_root",
    "install",
    "install_channel",
    "installed_names",
    "installed_runner",
    "list_lines",
    "locate_clis",
    "only_runner",
    "parse_acp_cli_kinds",
    "require_any_cli",
    "restart",
    "run_environment",
    "run_process",
    "service_definition",
    "start",
    "status_lines",
    "stop",
    "validate_name",
]

ROOT_ENV: Final = "AGENTIC_RUNNER_WORKSTATION_ROOT"

_LABEL_PREFIX: Final = "agentic-runner"
# The drain waits out an in-flight Directive (issue 02), as the chart's
# terminationGracePeriodSeconds does. Without it the service manager SIGKILLs the drain:
# systemd after 90 s, launchd after 20 s (QTS-2166), and launchd caps it at 60 s however
# high ExitTimeOut is set (QTS-2178), so on macOS `stop` and `restart` drain first.
DRAIN_TIMEOUT_SECONDS: Final = 2100

Run = Callable[..., subprocess.CompletedProcess[str]]


class CliMissingError(RuntimeError):
    pass


def validate_name(name: str) -> str:
    """One path segment, one launchd label, one systemd instance name -- all at once."""

    if not re.fullmatch(RUNNER_NAME_PATTERN, name):
        raise ValueError(
            f"Runner name {name!r} must be 1-32 of lowercase letters, digits and '-', "
            "starting with a letter or digit"
        )
    return name


def default_root(*, platform: str = sys.platform, environ: Mapping[str, str] = os.environ) -> Path:
    """Where every Runner's directory lives, per the OS's per-user convention."""

    if environ.get(ROOT_ENV, "").strip():
        return Path(environ[ROOT_ENV])
    home = Path(environ.get("HOME") or Path.home())
    if platform == "darwin":
        return home / "Library" / "Application Support" / "agentic-runner"
    if platform == "win32":
        return Path(environ.get("LOCALAPPDATA") or home / "AppData" / "Local") / "agentic-runner"
    state_home = environ.get("XDG_STATE_HOME", "").strip()
    return (Path(state_home) if state_home else home / ".local" / "state") / "agentic-runner"


def installed_names(root: Path) -> list[str]:
    """Every Runner installed under ``root``, by name.

    A directory whose name is not a Runner's is skipped: the guide moves an old install
    aside as ``<name>.organisation`` before a new token, and that one is not a Runner here.
    """

    if not root.is_dir():
        return []
    return sorted(
        path.name
        for path in root.iterdir()
        if re.fullmatch(RUNNER_NAME_PATTERN, path.name)
        and RunnerPaths(root=root, name=path.name).settings_file.is_file()
    )


def installed_runner(root: Path, name: str) -> RunnerPaths:
    """``<name>``, refused with the names installed here when it is not one of them."""

    names = installed_names(root)
    if name in names:
        return RunnerPaths(root=root, name=name)
    installed = ", ".join(names) if names else "none"
    raise ValueError(f"no Runner named {name!r} is installed under {root}; installed: {installed}")


def only_runner(root: Path, *, how: str = "name one") -> RunnerPaths:
    """The one Runner installed here, so a command needs no name (QTS-1774)."""

    names = installed_names(root)
    if len(names) == 1:
        return RunnerPaths(root=root, name=names[0])
    if not names:
        raise ValueError(f"no workstation Runner is installed under {root}")
    raise ValueError(
        f"this machine runs {', '.join(names)}: {how} (`agentic-runner list` shows each)"
    )


@dataclass(frozen=True)
class RunnerPaths:
    """``<root>/<name>/`` and everything the process keeps under it."""

    root: Path
    name: str

    @property
    def state_dir(self) -> Path:
        return self.root / self.name

    @property
    def hooks(self) -> Path:
        # Issue 45's path, per map ticket 26: `<state_dir>/<name>/hooks/`.
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
    def config_file(self) -> Path:
        # The host's own `config.toml`, where its `[limits]` table goes (ADR-0022 §57).
        # Never written by `install`: absent, the Runner limits nothing.
        return self.state_dir / "config.toml"

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
    def started_version(self) -> Path:
        # Written by the process, not `install`: every Runner here runs one console script,
        # and after `uv tool upgrade` a Runner not restarted still runs the old version.
        return self.state_dir / "runner-version"

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
            return Path(runtime) / f"agentic-runner-{self.name}"
        return self.state_dir / "sockets"

    def create(self) -> None:
        for directory in (self.state_dir, self.hooks, self.workspaces, self.contract_state):
            directory.mkdir(parents=True, exist_ok=True)
        # The identity's private key and the fallback credential files are in here.
        self.state_dir.chmod(0o700)


class WorkstationSettings(BaseModel):
    """What ``install`` was told, so ``run`` and ``status`` need no flags."""

    # `ignore`, not `forbid`: only `save` writes this file, and an install made before
    # local-agents 01 retired the per-process `cli_kind`, or before QTS-1773 dropped the
    # `org` its directory already names, still has to `run` and `status`.
    model_config = ConfigDict(extra="ignore", frozen=True)

    control_plane_url: str
    # A fallback only: `run` uses the address the registration response carries (ADR-0022
    # §4). An install made before 3.17 saved one and keeps it.
    temporal_address: str = ""
    # `temporal-grpc.<zone>` over TLS with the Runner Token (15's note on 23): no VPN, no
    # client certificate. Plaintext exists for a local dev server only.
    temporal_tls: bool = True
    # The PATH `install` ran under: a LaunchAgent starts with `/usr/bin:/bin:...` and
    # would not find a Homebrew or `~/.local/bin` CLI without it.
    path: str
    install_channel: InstallChannel
    # The Runner's `ACP_CLI_KINDS`, kept here rather than hand-added to the login agent,
    # which every `install` rewrites (QTS-1355).
    acp_cli_kinds: str = ""

    def save(self, paths: RunnerPaths) -> None:
        paths.settings_file.write_text(self.model_dump_json(indent=2), encoding="utf-8")

    @classmethod
    def load(cls, paths: RunnerPaths) -> WorkstationSettings:
        if not paths.settings_file.is_file():
            raise FileNotFoundError(
                f"no Runner is installed for {paths.name!r} under {paths.root}; "
                f"run `agentic-runner install {paths.name}` first"
            )
        return cls.model_validate_json(paths.settings_file.read_text(encoding="utf-8"))


def parse_acp_cli_kinds(value: str) -> str:
    """``--acp-cli-kinds``, refused if it names a kind with no pinned bridge.

    The Runner itself drops such a kind without a word, so a typo here would turn ACP off
    as quietly as the rewritten login agent did (QTS-1355).
    """

    kinds = [kind.strip() for kind in value.split(",") if kind.strip()]
    unknown = sorted(set(kinds) - ACP_BRIDGES.keys())
    if unknown:
        raise ValueError(
            f"--acp-cli-kinds names {', '.join(unknown)}; a pinned ACP bridge exists only for "
            f"{', '.join(sorted(ACP_BRIDGES))}"
        )
    return ",".join(dict.fromkeys(kinds))


def install_channel(
    platform: str = sys.platform, *, executable: str = sys.executable
) -> InstallChannel:
    if platform == "darwin":
        return InstallChannel.HOMEBREW if "/Cellar/" in executable else InstallChannel.MANUAL
    if platform == "win32":
        return InstallChannel.WINDOWS_USER
    return InstallChannel.SYSTEMD_USER


def run_environment(paths: RunnerPaths, settings: WorkstationSettings) -> dict[str, str]:
    """The environment ``agentic-runner run --name`` gives ``service.run``.

    Set here rather than in the service definition so one definition format -- a plist,
    a unit, a task -- stays a one-liner, and the three OSes cannot drift apart.
    """

    environment = {
        "PATH": settings.path,
        "AGENTIC_RUNNER_STATE_DIR": str(paths.state_dir),
        "AGENTIC_RUNNER_WORKSPACE_ROOT": str(paths.workspaces),
        "AGENTIC_RUNNER_HOOKS_PATH": str(paths.hooks),
        "AGENTIC_RUNNER_SOCKET_DIR": str(paths.socket_dir),
        "AGENTIC_RUNNER_ISOLATION": "none",
        CONFIG_FILE_ENV: str(paths.config_file),
        # A laptop has nothing beneath the attempt's egress proxy to stop a raw socket,
        # so it reports the Profile's allow-list as unenforced (PRD issue 58).
        service.EGRESS_POSTURE_ENV: EgressPosture.UNENFORCED.value,
        "AGENTIC_CONTROL_PLANE_URL": settings.control_plane_url,
        "AGENTIC_TEMPORAL_TLS": "1" if settings.temporal_tls else "0",
        "TEMPORAL_ADDRESS": settings.temporal_address,
        "WORKSPACE_ROOT": str(paths.workspaces),
        "WORKER_STATE_DIR": str(paths.contract_state),
        # Ephemeral: one process per Runner shares the host, so a fixed readiness
        # port inherited from a pod-style environment would collide on the second one.
        service.READINESS_PORT_ENV: "0",
    }
    # Only when set: a login agent hand-edited with `ACP_CLI_KINDS` before QTS-1355 keeps
    # ACP on across an upgrade until its next `install`.
    if settings.acp_cli_kinds:
        environment["ACP_CLI_KINDS"] = settings.acp_cli_kinds
    return environment


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
        version = parse_cli_version(answer.stdout or answer.stderr)
        if version is None:
            continue
        versions.append(
            CliVersion(cli_kind=cli_kind, version=version, meets_floor=meets_floor(name, version))
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
    restart: list[list[str]]
    # Sends SIGTERM outside the service manager's own stop, which `stop` and `restart` then
    # run only once the process has drained. Empty where that stop already waits it out.
    drain: list[list[str]] = field(default_factory=list)


def runner_command(name: str, root: Path) -> list[str]:
    # The console script as found, not resolved: Homebrew's `bin/agentic-runner` is a
    # symlink into a versioned Cellar path that the next `brew upgrade` deletes.
    script = shutil.which("agentic-runner")
    prefix = [script] if script else [sys.executable, "-m", "agentic_runner.launch"]
    return [*prefix, "run", "--name", name, "--root", str(root), "--login-agent"]


def service_definition(
    paths: RunnerPaths,
    settings: WorkstationSettings,
    *,
    platform: str = sys.platform,
    home: Path | None = None,
    user: str | None = None,
    uid: int | None = None,
    command: Sequence[str] | None = None,
) -> ServiceDefinition:
    home = home or Path.home()
    argv = list(command or runner_command(paths.name, paths.root))
    name = f"{_LABEL_PREFIX}.{paths.name}"
    if platform == "darwin":
        return _launch_agent(paths, settings, argv, name=name, home=home, uid=uid)
    if platform == "win32":
        return _logon_task(paths, argv, name=name, user=user)
    return _systemd_user_unit(paths, settings, argv, home=home, user=user)


def _launch_agent(
    paths: RunnerPaths,
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
            "ExitTimeOut": DRAIN_TIMEOUT_SECONDS,
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
        # One call, not bootout then bootstrap: launchd refuses a bootstrap of a label it
        # still holds, and it holds it until the old process has drained.
        restart=[["launchctl", "kickstart", "-k", f"{domain}/{name}"]],
        # launchd SIGKILLs a stop of its own after at most 60 s, whatever ExitTimeOut says,
        # but leaves alone a process that exits on a signal it only forwarded (CI, QTS-2178).
        # A clean exit is not restarted: KeepAlive above.
        drain=[["launchctl", "kill", "SIGTERM", f"{domain}/{name}"]],
    )


def _systemd_user_unit(
    paths: RunnerPaths,
    settings: WorkstationSettings,
    argv: list[str],
    *,
    home: Path,
    user: str | None,
) -> ServiceDefinition:
    unit_name = f"{_LABEL_PREFIX}-{paths.name}.service"
    unit = home / ".config" / "systemd" / "user" / unit_name
    exec_start = " ".join(_systemd_quote(arg) for arg in argv)
    content = (
        "[Unit]\n"
        f"Description=Agentic OS Runner for {paths.name}\n"
        "After=network-online.target\n\n"
        "[Service]\n"
        f"ExecStart={exec_start}\n"
        f"Environment={_systemd_quote('PATH=' + settings.path)}\n"
        "Restart=on-failure\n"
        "RestartSec=10\n"
        f"TimeoutStopSec={DRAIN_TIMEOUT_SECONDS}\n"
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
        restart=[[*systemctl, "restart", unit_name]],
    )


def _systemd_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _logon_task(
    paths: RunnerPaths, argv: list[str], *, name: str, user: str | None
) -> ServiceDefinition:
    task_xml = paths.state_dir / f"{name}.task.xml"
    task_name = f"\\agentic-runner\\{paths.name}"
    who = user or os.environ.get("USERNAME", "")
    arguments = subprocess.list2cmdline(argv[1:])
    content = f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Agentic OS Runner for {escape(paths.name)}</Description>
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
        restart=[
            ["schtasks", "/End", "/TN", task_name],
            ["schtasks", "/Run", "/TN", task_name],
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


def process_id(paths: RunnerPaths) -> int | None:
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


def heartbeat_age(paths: RunnerPaths, *, now: datetime | None = None) -> float | None:
    try:
        stamped = paths.heartbeat_stamp.stat().st_mtime
    except OSError:
        return None
    return max(0.0, (now or datetime.now(UTC)).timestamp() - stamped)


def status_lines(paths: RunnerPaths, *, now: datetime | None = None) -> list[str]:
    """``status <name>``: the process, its identity and its heartbeat age."""

    pid = process_id(paths)
    state = load_state(paths.state_dir)
    age = heartbeat_age(paths, now=now)
    lines = [
        f"runner name    {paths.name} ({paths.state_dir})",
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


_LIST_HEADER: Final = (
    "NAME",
    "ORGANISATION",
    "CONTRACT",
    "CONTROL PLANE",
    "VERSION",
    "LOGIN AGENT",
)


def list_lines(root: Path) -> list[str]:
    """``list``: one row per Runner installed here (QTS-1774).

    The Organisation's and the Contract's names once an ack from a control plane on 3.29
    named them (QTS-1909); before that, the first eight characters of their ids, as the
    console shows a Runner's id beside its name (Spec: Runners rev 7).
    """

    names = installed_names(root)
    if not names:
        return [f"no workstation Runner is installed under {root}"]
    rows = [
        _LIST_HEADER,
        *(_list_row_or_reason(RunnerPaths(root=root, name=name)) for name in names),
    ]
    widths = [max(len(row[column]) for row in rows) for column in range(len(_LIST_HEADER) - 1)]
    return [
        "  ".join([*(cell.ljust(width) for cell, width in zip(row, widths, strict=False)), row[-1]])
        for row in rows
    ]


def _list_row_or_reason(paths: RunnerPaths) -> tuple[str, str, str, str, str, str]:
    """One install that cannot be read must not hide the others' rows (QTS-1910).

    The first line only: a pydantic error runs to one line per field, and the row is one.
    """

    try:
        return _list_row(paths)
    except (OSError, ValueError, UnsafeStateError) as error:
        reason = str(error).splitlines()[0] if str(error) else type(error).__name__
        return (paths.name, "-", "-", "-", "-", f"unreadable: {reason}")


def _list_row(paths: RunnerPaths) -> tuple[str, str, str, str, str, str]:
    state = load_state(paths.state_dir)
    contracts = load_llm_contracts(paths.state_dir)
    organisation_name = _printable(load_organisation_name(paths.state_dir))
    try:
        version = paths.started_version.read_text(encoding="utf-8").strip() or "-"
    except OSError:
        version = "-"
    return (
        paths.name,
        (
            organisation_name
            or (state.temporal_namespace.removeprefix(NAMESPACE_PREFIX)[:8] if state else "-")
        ),
        (
            "-"
            if not contracts
            else _contract_label(contracts[0])
            if len(contracts) == 1
            else f"{len(contracts)} Contracts"
        ),
        WorkstationSettings.load(paths).control_plane_url,
        version,
        "running" if process_id(paths) else "stopped",
    )


def _contract_label(terms: ContractLlmTerms) -> str:
    return _printable(terms.contract_name) or str(terms.contract_id)[:8]


def _printable(name: str | None) -> str:
    # A name someone typed in the console: an escape sequence in it would reach the terminal.
    return "".join(character for character in name or "" if character.isprintable())


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
    paths: RunnerPaths,
    settings: WorkstationSettings,
    *,
    registration_token: str,
    pool_key: str | None = None,
    platform: str = sys.platform,
    home: Path | None = None,
    start_service: bool = True,
    run: Run = subprocess.run,
    http_client: httpx.AsyncClient | None = None,
) -> list[str]:
    """Register this Runner and install its login agent.

    Registration happens here, once, with the Runner Registration Token this user minted
    on /me/runners or the Organisation's Admin issued them (12 A6, QTS-1304) -- pasted at the
    prompt, never stored: the identity bootstrap hands back is what every later start uses,
    and a lost state directory means a reinstall and a re-delivery of any sealed value
    (22 A4), by design.

    ``pool_key`` is the Organisation's Pool Key, for a Runner of its pool that opens the
    pool's LLM Keys (Spec: Runners rev 3). Unlike the token it is kept, 0600 in the state
    directory, because every start of the Runner needs it.
    """

    require_any_cli(settings.path)
    if pool_key:
        # Checked before anything registers; kept only once something has.
        parse_pool_key(pool_key)
    paths.create()
    settings.save(paths)
    already_registered = load_state(paths.state_dir) is not None
    state, outcome = await service.register(
        state_dir=paths.state_dir,
        can_separate_uids=False,
        client=http_client,
        control_plane=settings.control_plane_url,
        registration_token=registration_token,
        isolation=IsolationMode.NONE,
    )
    if pool_key:
        save_pool_key(paths.state_dir, pool_key)
    LifecycleOutbox(paths.state_dir).add(LifecycleKind.INSTALL, datetime.now(UTC))
    definition = service_definition(paths, settings, platform=platform, home=home)
    definition.path.parent.mkdir(parents=True, exist_ok=True)
    definition.path.write_bytes(definition.content)
    lines = [outcome]
    if already_registered:
        # QTS-1314: the dogfood reinstall kept an Organisation-hosted identity while the
        # person believed the user-hosted token they had just pasted had taken effect.
        lines.append(
            f"token          not used: {paths.state_dir} already holds Runner "
            f"{state.runner_id}; to register a new one, stop it and move that directory "
            "aside (docs/workstation.md, Switch to a new token)"
        )
    lines.append(f"login agent    {definition.path}")
    if start_service:
        drive(definition.install + definition.start, run=run)
        lines.append(f"started        {state.runner_id}")
        lines.extend(next_step_lines(state.host_party))
    return lines


def start(paths: RunnerPaths, **driver: Any) -> None:
    _drive_service(paths, "start", **driver)


def stop(paths: RunnerPaths, **driver: Any) -> None:
    """Stop this Runner; every other Runner on this machine keeps polling."""

    _drive_service(paths, "stop", **driver)


def restart(
    paths: RunnerPaths,
    *,
    timeout: float = 60.0,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    **driver: Any,
) -> tuple[int, str]:
    """Restart a running Runner and wait for its new process: ``(pid, version)``.

    ``uv tool upgrade`` replaces the console script, not the process, so a login agent keeps
    running the old version until this (QTS-2158). The process writes its version before
    its pidfile, so a new pid means the version beside it is the new process's.
    """

    previous = process_id(paths)
    _drive_service(paths, "restart", sleep=sleep, clock=clock, **driver)
    # Taken after the call: the drain and then `kickstart -k`, or `systemctl restart`, return
    # only once the old process has exited, so the drain spends none of it (QTS-2172).
    deadline = clock() + timeout
    while (pid := process_id(paths)) is None or pid == previous:
        if clock() >= deadline:
            raise RuntimeError(
                f"no new process wrote {paths.pidfile} within {timeout:.0f}s; see {paths.log}"
            )
        sleep(0.5)
    return pid, paths.started_version.read_text(encoding="utf-8").strip()


def _drive_service(
    paths: RunnerPaths,
    verb: Literal["start", "stop", "restart"],
    *,
    platform: str = sys.platform,
    home: Path | None = None,
    run: Run = subprocess.run,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    settings = WorkstationSettings.load(paths)
    definition = service_definition(paths, settings, platform=platform, home=home)
    if verb != "start" and definition.drain and process_id(paths) is not None:
        drive(definition.drain, run=run)
        # Past the budget the service manager's stop below ends it, as systemd's would.
        deadline = clock() + DRAIN_TIMEOUT_SECONDS
        while process_id(paths) is not None and clock() < deadline:
            sleep(1.0)
    drive(getattr(definition, verb), run=run)


def run_process(paths: RunnerPaths, *, login_agent: bool) -> int:
    """``agentic-runner run --name``: what the login agent executes."""

    settings = WorkstationSettings.load(paths)
    try:
        require_any_cli(settings.path)
    except CliMissingError as error:
        print(f"refusing to start (cli_missing): {error}", flush=True)
        return 1
    for name, value in run_environment(paths, settings).items():
        os.environ[name] = value
    paths.started_version.write_text(runner_version, encoding="utf-8")
    store = open_workstation_store(paths.name, fallback=paths.credentials)
    attestation = collect_attestation(
        channel=settings.install_channel,
        session=SessionKind.LOGIN_AGENT if login_agent else SessionKind.INTERACTIVE,
        store=store.kind,
        path=settings.path,
    )
    # A workstation's hostname can name its person, so it is never reported (rev 3); the
    # name it was installed as is the person's choice, and the console lists it (QTS-1773).
    install = RunnerInstall(target=InstallTarget.WORKSTATION, name=paths.name)
    return asyncio.run(service.run(host_store=store, attestation=attestation, install=install))
