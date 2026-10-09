"""A Profile-named ACP command, through the real activity (local-agents 18).

What only the activity can show: the command reaches the Agent Runtime on the request for
a kind with no pinned bridge, and one sent for a pinned kind is refused non-retryably with
Evidence naming the kind and the executable -- never an argument -- and spawns nothing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from temporalio.exceptions import ApplicationError
from test_runner_auth_mode_directives import (
    _activities,
    _branch_pr_input,
    _Client,
    _Codex,
)

from agentic_runner.activities import AGENT_RUNTIME_EVIDENCE_SOURCE, RunnerRalphActivities

COMMAND = ["/opt/acp/gemini", "--experimental-acp", "--api-key=sk-live-0123456789abcdef"]


class _ProfileClient(_Client):
    def __init__(self, cli_kind: str) -> None:
        super().__init__()
        self.cli_kind = cli_kind

    async def get_runtime_context(self, work_record_id: str) -> dict[str, Any]:
        context = await super().get_runtime_context(work_record_id)
        return {**context, "cli_kind": self.cli_kind, "acp_command": COMMAND}


def _serving(
    client: _Client, runtime: _Codex, cli_kind: str, tmp_path: Path
) -> RunnerRalphActivities:
    return _activities(client, runtime, tmp_path=tmp_path, host_party="user", cli_kind=cli_kind)


@pytest.mark.asyncio
async def test_the_profile_named_command_reaches_the_runtime_for_an_unpinned_kind(
    tmp_path: Path,
) -> None:
    runtime = _Codex()

    await _serving(
        _ProfileClient("gemini_cli"), runtime, "gemini_cli", tmp_path
    ).create_or_update_branch_pr(_branch_pr_input("user"))

    [request] = runtime.requests
    assert request.acp_command == tuple(COMMAND)


@pytest.mark.parametrize("cli_kind", ["codex_cli", "claude_code"])
@pytest.mark.asyncio
async def test_a_profile_named_command_for_a_pinned_kind_is_refused_with_evidence(
    tmp_path: Path, cli_kind: str
) -> None:
    client = _ProfileClient(cli_kind)
    runtime = _Codex()

    with pytest.raises(ApplicationError) as refused:
        await _serving(client, runtime, cli_kind, tmp_path).create_or_update_branch_pr(
            _branch_pr_input("user")
        )

    assert refused.value.non_retryable is True
    assert runtime.requests == [], "nothing is spawned for a refused command"
    [evidence] = client.payloads(AGENT_RUNTIME_EVIDENCE_SOURCE)
    assert evidence == {
        "event": "directive.acp_command_refused",
        "cli_kind": cli_kind,
        "executable": "gemini",
    }
    assert "sk-live" not in repr(client.evidence)
