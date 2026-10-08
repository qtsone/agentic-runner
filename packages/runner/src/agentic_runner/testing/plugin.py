"""The conformance kit's pytest plugin (``pytest11`` entry point ``agentic_runner``).

Fixtures a fork's own tests can use as well as the scenarios in ``test_conformance.py``:
``fake_control_plane`` serves :class:`FakeControlPlane` on a free port, and
``runner_launcher`` starts the *installed* Runner -- ``agentic-runner run``, a real
process -- against it, so a scenario sees exactly what an operator's process would do.

Imports nothing beyond pytest and the Runner's own dependencies: the entry point loads
this module into every pytest session of an environment that has the Runner installed,
with or without the ``testing`` extra.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from temporalio.testing import WorkflowEnvironment

from agentic_runner.testing.control_plane import DEFAULT_NAMESPACE, FakeControlPlane

# A conformance Runner never runs a Directive that needs isolation or a credential, so it
# runs `none`, unprivileged, with every path under the test's own directory.
AGENT_TOKEN = "conformance-agent-token-0123456789"
_RUN = "import sys; from agentic_runner.cli import main; sys.exit(main(['run']))"


@dataclass(frozen=True)
class ServedControlPlane:
    plane: FakeControlPlane
    url: str


@dataclass
class RunnerProcess:
    """One ``agentic-runner run`` process; its stdout and stderr go to ``log``."""

    process: subprocess.Popen[bytes]
    log: Path
    readiness_port: int

    def ready(self) -> bool:
        """The readiness probe the chart points Kubernetes at: registered, heard, polling."""

        url = f"http://127.0.0.1:{self.readiness_port}/healthz"
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                return bool(response.status == 200)
        except (urllib.error.URLError, OSError):
            return False

    def output(self) -> str:
        return self.log.read_text(encoding="utf-8", errors="replace")

    def running(self) -> bool:
        return self.process.poll() is None

    def wait(self, timeout: float) -> int:
        return self.process.wait(timeout=timeout)

    def terminate(self, timeout: float = 60) -> int:
        """SIGTERM -- what Kubernetes, ``docker stop`` and launchd send -- then wait."""

        self.process.send_signal(signal.SIGTERM)
        return self.wait(timeout)


@dataclass
class RunnerLauncher:
    """Starts Runners that share one state directory, so a restart is the same Runner."""

    root: Path
    started: list[RunnerProcess] = field(default_factory=list)

    def start(self, *, control_plane_url: str, temporal_address: str) -> RunnerProcess:
        state = self.root / "state"
        readiness_port = _free_port()
        environment = {
            **os.environ,
            "AGENTIC_CONTROL_PLANE_URL": control_plane_url,
            "AGENTIC_AGENT_TOKEN": AGENT_TOKEN,
            "AGENTIC_RUNNER_ISOLATION": "none",
            "AGENTIC_RUNNER_STATE_DIR": str(state),
            "AGENTIC_RUNNER_WORKSPACE_ROOT": str(self.root / "workspaces"),
            "AGENTIC_RUNNER_SOCKET_DIR": str(self.root / "sockets"),
            "AGENTIC_RUNNER_CONFIG": str(self.root / "absent-config.toml"),
            "AGENTIC_RUNNER_READINESS_PORT": str(readiness_port),
            "TEMPORAL_ADDRESS": temporal_address,
            "AGENTIC_TEMPORAL_TLS": "false",
            "WORKSPACE_ROOT": str(self.root / "workspaces"),
            "WORKER_STATE_DIR": str(state),
            "CODEX_HOME": str(self.root / "codex"),
        }
        log = self.root / f"runner-{len(self.started)}.log"
        with log.open("wb") as sink:
            process = subprocess.Popen(
                [sys.executable, "-c", _RUN],
                env=environment,
                stdout=sink,
                stderr=subprocess.STDOUT,
            )
        runner = RunnerProcess(process=process, log=log, readiness_port=readiness_port)
        self.started.append(runner)
        return runner

    def close(self) -> None:
        for runner in self.started:
            if runner.running():
                runner.process.kill()
                runner.process.wait()
            print(f"--- {runner.log.name} (exit {runner.process.returncode})")
            print(runner.output())


async def start_temporal(namespace: str = DEFAULT_NAMESPACE) -> WorkflowEnvironment:
    """A local Temporal dev server holding ``namespace``.

    Uses a ``temporal`` CLI already on ``PATH`` when there is one; otherwise the SDK
    downloads the dev server on first use.
    """

    return await WorkflowEnvironment.start_local(
        namespace=namespace, dev_server_existing_path=shutil.which("temporal")
    )


async def until_ready(served: ServedControlPlane, runner: RunnerProcess, beats: int = 1) -> None:
    """Wait until the Runner is ready and the fake has heard ``beats`` heartbeats.

    Ready, not merely registered: the drain handler is installed once the Worker polls,
    and a scenario that signals before then is testing start-up, not the drain. Fails at
    once -- with the Runner's output -- if the process dies first.
    """

    plane = served.plane
    deadline = time.monotonic() + 120
    while len(plane.heartbeats) < beats or not runner.ready():
        if not runner.running():
            pytest.fail(f"the Runner exited {runner.process.returncode}:\n{runner.output()}")
        if time.monotonic() > deadline:
            pytest.fail(f"not ready in 120s; the fake saw {plane.stats()}:\n{runner.output()}")
        await asyncio.sleep(0.1)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture
def fake_control_plane() -> Iterator[ServedControlPlane]:
    """The fake, served over HTTP, handing out a one-second heartbeat cadence."""

    plane = FakeControlPlane(heartbeat_interval_seconds=1)
    with plane.serving() as url:
        yield ServedControlPlane(plane=plane, url=url)


@pytest.fixture
def runner_launcher(tmp_path: Path) -> Iterator[RunnerLauncher]:
    launcher = RunnerLauncher(root=tmp_path)
    try:
        yield launcher
    finally:
        launcher.close()
