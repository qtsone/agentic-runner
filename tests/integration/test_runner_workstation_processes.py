"""Two Organisations on one workstation (PRD issue 47, map ticket 23 item 3).

A contractor with two clients installs twice and runs two processes -- no supervisor
over N Organisations. Each ``install`` registers against a fake control plane (served
over real HTTP) and each ``agentic-runner run --org`` is a real OS process, exactly as
its login agent would exec it, polling a Temporal dev server. What this pins:

* two state directories, two identities, two Runner Tokens;
* ``stop`` on one -- the ``SIGTERM`` ``launchctl bootout`` / ``systemctl --user stop``
  deliver -- drains that process, which reports ``stop`` on its way out, while the other
  keeps heartbeating and keeps polling its own ``runner.{runner_id}`` queue;
* ``status <org>`` reads the survivor as running with a fresh heartbeat.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from temporalio.api.enums.v1 import TaskQueueType
from temporalio.api.taskqueue.v1 import TaskQueue
from temporalio.api.workflowservice.v1 import DescribeTaskQueueRequest
from temporalio.testing import WorkflowEnvironment

from agentic_runner import workstation
from agentic_runner.registration import load_state
from agentic_runner_contracts import __version__ as contracts_version
from agentic_runner_contracts.runner_registration import InstallChannel


class Plane:
    """Bootstrap and heartbeat over real HTTP; one Runner Token minted per Runner."""

    def __init__(self) -> None:
        self.tokens: dict[str, str] = {}
        self.heartbeats: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.lock = threading.Lock()

    def handler(self) -> type[BaseHTTPRequestHandler]:
        plane = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_: Any) -> None:
                return

            def do_POST(self) -> None:  # noqa: N802 - http.server's contract
                body = json.loads(self.rfile.read(int(self.headers["content-length"])))
                expires = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
                with plane.lock:
                    if self.path.endswith("/bootstrap"):
                        runner_id = str(uuid4())
                        plane.tokens[runner_id] = f"runner-token-{uuid4()}"
                        answer: dict[str, Any] = {
                            "identity": {
                                "runner_id": runner_id,
                                "identity_id": f"id-{runner_id}",
                                "private_key_pem": _pem(),
                            },
                            "temporal_namespace": "default",
                            "task_queue": f"runner.{runner_id}",
                            "runner_token": plane.tokens[runner_id],
                            "runner_token_expires_at": expires,
                            "tag_set_version": 1,
                            "floor_state": "ok",
                            "contracts_floor": contracts_version,
                            "host_party": "user",
                            "heartbeat_interval_seconds": 1,
                        }
                    else:
                        runner_id = self.headers["X-Runner-Id"]
                        plane.heartbeats[runner_id].append(body)
                        answer = {
                            "runner_id": runner_id,
                            "runner_token": plane.tokens[runner_id],
                            "runner_token_expires_at": expires,
                            "floor_state": "ok",
                            "contracts_floor": contracts_version,
                            "tag_set_version": 1,
                            "accepts_new_directives": True,
                        }
                raw = json.dumps(answer).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        return Handler

    def beats(self, runner_id: str) -> int:
        with self.lock:
            return len(self.heartbeats[runner_id])


def _pem() -> str:
    return (
        Ed25519PrivateKey.generate()
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        .decode()
    )


async def _wait_for(predicate: Any, *, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while not await predicate():
        assert time.monotonic() < deadline, "timed out waiting"
        await asyncio.sleep(0.2)


@pytest.mark.asyncio
async def test_two_organisations_run_as_two_processes_and_stopping_one_leaves_the_other(
    tmp_path: Path,
) -> None:
    plane = Plane()
    server = ThreadingHTTPServer(("127.0.0.1", 0), plane.handler())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    control_plane = f"http://127.0.0.1:{server.server_address[1]}"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "codex").write_text("#!/bin/sh\necho 'codex-cli 0.141.0'\n")
    (bin_dir / "codex").chmod(0o755)
    # Only the fake CLI: a developer's real `claude` / `codex` must not leak in.
    path = os.pathsep.join([str(bin_dir), "/usr/bin", "/bin"])
    root = tmp_path / "state"
    processes: dict[str, subprocess.Popen[bytes]] = {}

    async with await WorkflowEnvironment.start_local() as env:
        orgs = {org: workstation.OrgPaths(root=root, org=org) for org in ("acme", "globex")}
        for org, paths in orgs.items():
            await workstation.install(
                paths,
                workstation.WorkstationSettings(
                    org=org,
                    control_plane_url=control_plane,
                    temporal_address=env.client.service_client.config.target_host,
                    temporal_tls=False,
                    path=path,
                    install_channel=InstallChannel.MANUAL,
                ),
                agent_token=f"agent-token-issued-to-me-for-{org}",
                home=tmp_path / "home",
                start_service=False,
            )
        states = {org: load_state(paths.state_dir) for org, paths in orgs.items()}
        runner_ids = {org: str(state.runner_id) for org, state in states.items() if state}
        try:
            for org in orgs:
                # What each login agent execs.
                processes[org] = subprocess.Popen(
                    [sys.executable, "-m", "agentic_runner.cli", "run", "--org", org]
                    + ["--root", str(root), "--login-agent"],
                    # Its own runtime dir, so a concurrent run of this test cannot share
                    # the per-Organisation socket directory.
                    env={**os.environ, "PATH": path, "XDG_RUNTIME_DIR": str(tmp_path)},
                )

            async def both_polling() -> bool:
                polled = [await _polled(env, f"runner.{rid}") for rid in runner_ids.values()]
                return all(polled) and all(plane.beats(rid) >= 2 for rid in runner_ids.values())

            await _wait_for(both_polling)

            # `stop acme`: the SIGTERM its service manager delivers.
            processes["acme"].send_signal(signal.SIGTERM)
            assert processes["acme"].wait(timeout=60) == 0
            acme_beats = plane.beats(runner_ids["acme"])
            globex_beats = plane.beats(runner_ids["globex"])

            async def globex_still_heartbeating() -> bool:
                return plane.beats(runner_ids["globex"]) >= globex_beats + 3

            await _wait_for(globex_still_heartbeating)
            assert processes["globex"].poll() is None
            assert plane.beats(runner_ids["acme"]) == acme_beats
            assert await _polled(env, f"runner.{runner_ids['globex']}")
            status = workstation.status_lines(orgs["globex"])
            assert f"running, pid {processes['globex'].pid}" in status[1]
            assert "not running" in workstation.status_lines(orgs["acme"])[1]
        finally:
            for process in processes.values():
                if process.poll() is None:
                    process.send_signal(signal.SIGTERM)
                    process.wait(timeout=60)
            server.shutdown()

    # Two state directories, two identities, two Runner Tokens.
    assert orgs["acme"].state_dir != orgs["globex"].state_dir
    assert len(set(runner_ids.values())) == 2
    assert len({plane.tokens[rid] for rid in runner_ids.values()}) == 2
    # Each process spoke only as itself, as a workstation: `none`, attested.
    for org, rid in runner_ids.items():
        first = plane.heartbeats[rid][0]
        assert first["isolation_mode"] == "none"
        assert first["hosted_task_queue"] == f"runner.{rid}"
        assert first["attestation"]["session_kind"] == "login_agent"
        assert [cli["cli_kind"] for cli in first["attestation"]["clis"]] == ["codex_cli"]
        kinds = [event["kind"] for beat in plane.heartbeats[rid] for event in beat["lifecycle"]]
        assert kinds[:2] == ["install", "start"], org
    acme_kinds = [
        event["kind"]
        for beat in plane.heartbeats[runner_ids["acme"]]
        for event in beat["lifecycle"]
    ]
    assert acme_kinds[-1] == "stop"


async def _polled(env: WorkflowEnvironment, queue: str, *, within: float = 90.0) -> bool:
    """Whether some worker long-polled ``queue``'s activity side in the last ``within`` s.

    A poller's access time is stamped when its long poll *starts*, and a long poll lasts
    up to a minute, so the window is wider than that.
    """

    described = await env.client.workflow_service.describe_task_queue(
        DescribeTaskQueueRequest(
            namespace="default",
            task_queue=TaskQueue(name=queue),
            task_queue_type=TaskQueueType.TASK_QUEUE_TYPE_ACTIVITY,
        )
    )
    now = time.time()
    return any(now - poller.last_access_time.seconds <= within for poller in described.pollers)
