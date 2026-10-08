"""A sandboxed spawn starts on the host it runs on, macOS included (QTS-1319).

``test_contract_uid_isolation.py`` proves the floor binds, but only as root on Linux. This
one needs no privilege, so the macOS Workstation job runs it too: before QTS-1319 every
sandboxed spawn there failed in ``preexec_fn``, because XNU refuses ``RLIMIT_DATA``.
"""

from __future__ import annotations

import logging
import resource
import sys
from pathlib import Path

import pytest

from agentic_runner.workers._runtime_support import run_subprocess_exec
from agentic_runner.workers.contract_isolation import ContractIsolation

CONTRACT = "11111111-2222-4333-8444-555555555555"
MEMORY_LIMIT_BYTES = 2 * 1024**3

_READ_LIMITS = (
    "import resource; "
    "print(resource.getrlimit(resource.RLIMIT_NPROC)[1], "
    "resource.getrlimit(resource.RLIMIT_DATA)[1])"
)


def _isolation(tmp_path: Path, *, can_limit_memory: bool | None = None) -> ContractIsolation:
    return ContractIsolation(
        workspace_root=tmp_path / "workspaces",
        state_dir=tmp_path / "state",
        uid_min=60_000,
        uid_max=60_009,
        max_processes=256,
        memory_limit_bytes=MEMORY_LIMIT_BYTES,
        can_separate_uids=False,
        can_limit_memory=can_limit_memory,
    )


@pytest.mark.asyncio
async def test_a_sandboxed_spawn_runs_under_the_floor_this_host_can_hold(
    tmp_path: Path,
) -> None:
    isolation = _isolation(tmp_path)
    workspace = isolation.prepare_workspace(CONTRACT, "123e4567-e89b-12d3-a456-426614174000")

    result = await run_subprocess_exec(
        argv=[sys.executable, "-c", _READ_LIMITS],
        cwd=workspace,
        env={},
        stdin=None,
        timeout_seconds=30,
        output_limit_bytes=4096,
        sandbox=isolation.sandbox(CONTRACT, runtime_kind="claude_cli"),
    )

    assert result.exit_code == 0, result.stderr
    max_processes, max_data = (int(value) for value in result.stdout.split())
    assert max_processes == 256
    if sys.platform == "darwin":
        assert not isolation.can_limit_memory
        assert max_data == resource.RLIM_INFINITY
    else:
        assert isolation.can_limit_memory
        assert max_data == MEMORY_LIMIT_BYTES


def test_a_host_without_a_memory_ceiling_says_so_and_spawns_without_one(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        isolation = _isolation(tmp_path, can_limit_memory=False)

    sandbox = isolation.sandbox(
        CONTRACT, runtime_kind="claude_cli", memory_limit_bytes=MEMORY_LIMIT_BYTES
    )

    assert sandbox.max_memory_bytes is None
    assert f"without the {MEMORY_LIMIT_BYTES}-byte memory ceiling" in caplog.text
