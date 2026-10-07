"""Scoped egress for a Directive (PRD issue 58, map 06 §4, ticket 05's open item).

The Helm shape: a Directive under a Profile egress allow-list is started with the
attempt's proxy variables, and a real child process following them cannot reach a planted
host the list does not name -- while a listed one answers. Name resolution is faked so
both "hosts" are this test's own loopback listeners: what is under test is the Runner's
decision, not the internet. A workstation reports the same list as ``unenforced``.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from pathlib import Path
from typing import Any

import pytest

from agentic_runner import service
from agentic_runner.activities import RunnerRalphActivities, _RuntimeContextState
from agentic_runner.callback import CALLBACK_TOKEN_ENV
from agentic_runner.egress import EGRESS_REFUSED_SOURCE, destination_allowed
from agentic_runner.mcp import McpPlan
from agentic_runner.workers._runtime_support import apply_extra_env
from agentic_runner.workstation import OrgPaths, WorkstationSettings, run_environment
from agentic_runner_contracts.runner_registration import EgressPosture, InstallChannel

LISTED = "api.listed.example"
PLANTED = "planted.unlisted.example"

_FETCH = """
import sys, urllib.request, urllib.error
try:
    print(urllib.request.urlopen(sys.argv[1], timeout=10).read().decode())
except urllib.error.HTTPError as error:
    print(f"refused {error.code}")
"""


class _EvidenceRecordingClient:
    def __init__(self) -> None:
        self.appended: list[tuple[str, dict[str, Any]]] = []

    async def append_evidence(
        self, work_record_id: str, *, source: str, payload: dict[str, Any]
    ) -> None:
        self.appended.append((source, payload))


async def _origin(body: bytes) -> tuple[asyncio.Server, int]:
    async def answer(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s"
            % (len(body), body)
        )
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(answer, host="127.0.0.1", port=0)
    return server, int(server.sockets[0].getsockname()[1])


async def _fetch(url: str, env: dict[str, str]) -> str:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        _FETCH,
        url,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
    return (stdout or stderr).decode().strip()


@pytest.mark.asyncio
async def test_a_directive_under_a_profile_allow_list_cannot_reach_an_unlisted_host() -> None:
    listed, listed_port = await _origin(b"listed-origin")
    planted, planted_port = await _origin(b"planted-origin")
    ports = {LISTED: listed_port, PLANTED: planted_port}

    async def resolver(host: str, port: int) -> tuple[str, int]:
        return "127.0.0.1", ports[host]

    client = _EvidenceRecordingClient()
    activities = RunnerRalphActivities(client, egress_resolver=resolver)  # type: ignore[arg-type]
    state = _RuntimeContextState(
        reviewer=None,
        repository="acme/repo",
        verifier_argv=("true",),
        work_branch="agent/work",
        completion_criteria="do the work",
        contract_id="contract-1",
        egress_allow_list=(LISTED,),
    )
    env = {CALLBACK_TOKEN_ENV: "attempt-bearer"}
    try:
        async with contextlib.AsyncExitStack() as stack:
            _, allow_list = await activities._start_mcp_and_egress(
                stack,
                work_record_id="wr-1",
                state=state,
                plan=McpPlan(),
                credentials=None,
                env=env,
            )
            # The Profile's list plus what the Runner knows the Directive needs.
            assert allow_list == (LISTED, "github.com")
            # The same merge the runtimes apply: the proxy variables survive it, so the
            # Agent Runtime subprocess is started pointed at the proxy.
            child_env = apply_extra_env({}, sorted(env.items()))
            assert child_env["HTTP_PROXY"] == child_env["https_proxy"]

            reached = await _fetch(f"http://{LISTED}/", child_env)
            blocked = await _fetch(f"http://{PLANTED}/", child_env)
    finally:
        listed.close()
        planted.close()

    assert reached == "listed-origin"
    assert blocked == "refused 403"
    assert (EGRESS_REFUSED_SOURCE, {"event": "egress.refused", "host": PLANTED, "port": 80}) in (
        client.appended
    )


@pytest.mark.asyncio
async def test_the_proxy_refuses_a_caller_without_the_attempt_bearer() -> None:
    client = _EvidenceRecordingClient()
    activities = RunnerRalphActivities(client)  # type: ignore[arg-type]
    state = _RuntimeContextState(
        reviewer=None,
        repository="acme/repo",
        verifier_argv=("true",),
        work_branch="agent/work",
        completion_criteria="do the work",
        egress_allow_list=(LISTED,),
    )
    env: dict[str, str] = {}
    async with contextlib.AsyncExitStack() as stack:
        await activities._start_mcp_and_egress(
            stack, work_record_id="wr-1", state=state, plan=McpPlan(), credentials=None, env=env
        )
        # Another Contract's uid on the same loopback, holding the address but not the
        # bearer, borrows nothing.
        stripped = env["HTTP_PROXY"].replace(f"agentic:{env[CALLBACK_TOKEN_ENV]}@", "")
        answer = await _fetch(f"http://{LISTED}/", {"HTTP_PROXY": stripped})

    assert answer == "refused 407"


def test_allow_list_entries_match_host_port_and_subdomain_patterns() -> None:
    assert destination_allowed(["github.com"], "github.com", 443)
    assert destination_allowed(["github.com:443"], "github.com", 443)
    assert not destination_allowed(["github.com:443"], "github.com", 22)
    assert destination_allowed(["*.githubusercontent.com"], "raw.githubusercontent.com", 443)
    assert not destination_allowed(["github.com"], "evil-github.com", 443)


def test_a_workstation_reports_its_egress_posture_as_unenforced(tmp_path: Path) -> None:
    """The host cannot hold a raw socket to the list, so it does not claim to."""

    environment = run_environment(
        OrgPaths(root=tmp_path, org="acme"),
        WorkstationSettings(
            org="acme",
            control_plane_url="https://control.example",
            temporal_address="temporal.example:7233",
            cli_kind="codex_cli",
            path="/usr/bin",
            install_channel=InstallChannel.HOMEBREW,
        ),
    )

    assert EgressPosture(environment[service.EGRESS_POSTURE_ENV]) is EgressPosture.UNENFORCED
