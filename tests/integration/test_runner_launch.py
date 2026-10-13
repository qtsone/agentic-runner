"""A SIGTERM during start-up stops ``agentic-runner run`` at once, with exit 0 (QTS-1536).

A real process, exec'd the way ``runner_command`` falls back without the console script,
against a control plane that accepts the connection and never answers: start-up hangs
on registration for as long as the test lets it. In a container the Runner is PID 1 and
the kernel drops a signal PID 1 has no handler for; here, without one, the process dies
of the signal instead -- either way not the clean exit ``docker stop`` waits for.

0.3 s lands in the CLI's imports, which take seconds; 3 s lands in registration.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture
def silent_control_plane() -> Iterator[str]:
    """Listens and never accepts: the kernel completes the handshake, nothing answers."""

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(8)
        yield f"http://127.0.0.1:{listener.getsockname()[1]}"


@pytest.mark.parametrize("delay", [0.3, 3.0])
def test_a_sigterm_before_the_worker_starts_exits_zero_at_once(
    tmp_path: Path, silent_control_plane: str, delay: float
) -> None:
    environment = {
        **os.environ,
        "AGENTIC_CONTROL_PLANE_URL": silent_control_plane,
        "AGENTIC_RUNNER_REGISTRATION_TOKEN": "launch-test-registration-token-0123",
        "AGENTIC_RUNNER_ISOLATION": "none",
        "AGENTIC_RUNNER_STATE_DIR": str(tmp_path / "state"),
        "AGENTIC_RUNNER_WORKSPACE_ROOT": str(tmp_path / "workspaces"),
        "AGENTIC_RUNNER_SOCKET_DIR": str(tmp_path / "sockets"),
        "AGENTIC_RUNNER_CONFIG": str(tmp_path / "absent-config.toml"),
        "TEMPORAL_ADDRESS": "127.0.0.1:1",
        "WORKSPACE_ROOT": str(tmp_path / "workspaces"),
        "WORKER_STATE_DIR": str(tmp_path / "state"),
    }
    process = subprocess.Popen(
        [sys.executable, "-m", "agentic_runner.launch", "run"],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        time.sleep(delay)
        assert process.poll() is None, process.communicate()[0].decode()
        process.send_signal(signal.SIGTERM)
        output, _ = process.communicate(timeout=10)
    finally:
        process.kill()

    assert process.returncode == 0, output.decode()
