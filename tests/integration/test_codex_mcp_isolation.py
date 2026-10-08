"""A Codex Directive starts the Runner's MCP servers and no others (LA-19, ADR-0015).

Drives the real ``codex`` binary through ``CodexRuntime``: a fake ``codex`` would only test
our model of Codex's config layering, and that model is what was wrong. Every MCP server
here is a script that appends its name to a marker file and exits, so the file lists
what Codex tried to start. Codex starts its servers before its first model call, and the
dead ``HTTPS_PROXY`` makes that call fail locally instead of reaching OpenAI.

Runs only against the version ``Dockerfile.runner`` pins (``CODEX_CLI_VERSION``): the flags
relied on are version-specific, and codex-cli 0.159.2 hangs on the dead proxy instead of
failing. Bumping the pin means re-running this with the new binary on ``PATH``.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from agentic_runner.workers.agent_runtime import AuthMode, DirectiveRequest
from agentic_runner.workers.codex_runtime import CodexRuntime
from agentic_runner.workers.mcp_config import McpServerEntry
from agentic_runner.workers.settings import WorkerSettings

_PINNED = re.search(
    r"CODEX_CLI_VERSION=(\S+)",
    (Path(__file__).resolve().parents[2] / "Dockerfile.runner").read_text(),
)


def _installed_codex() -> str | None:
    if shutil.which("codex") is None:
        return None
    return subprocess.run(["codex", "--version"], capture_output=True, text=True).stdout.split()[-1]


pytestmark = pytest.mark.skipif(
    _PINNED is None or _installed_codex() != _PINNED.group(1),
    reason="needs the pinned codex CLI (Dockerfile.runner CODEX_CLI_VERSION) on PATH",
)

_UNREACHABLE_PROXY = "http://127.0.0.1:9"


def _marker_server(tmp_path: Path) -> Path:
    markers = tmp_path / "markers.txt"
    script = tmp_path / "mcp-marker.sh"
    script.write_text(f'#!/bin/sh\necho "$1" >> {markers}\n')
    script.chmod(0o755)
    return script


def _server_table(slug: str, script: Path) -> str:
    return f'[mcp_servers.{slug}]\ncommand = "{script}"\nargs = ["{slug}"]\n'


def _started(tmp_path: Path) -> set[str]:
    markers = tmp_path / "markers.txt"
    return set(markers.read_text().split()) if markers.exists() else set()


async def _run_directive(
    tmp_path: Path, *, harness_config: str, mcp_servers: tuple[McpServerEntry, ...] | None
) -> None:
    script = _marker_server(tmp_path)
    settings = WorkerSettings(
        TEMPORAL_ADDRESS="127.0.0.1:7233",
        INTERNAL_FASTAPI_BASE_URL="http://agentic-api.internal:8000",
        WORKSPACE_ROOT=tmp_path / "workspaces",
        CODEX_HOME=tmp_path / "codex-home",
        CODEX_CLI_TIMEOUT_SECONDS=120,
        CODEX_SANDBOX_MODE="danger-full-access",
        CODEX_ASK_FOR_APPROVAL="never",
        CODEX_STRICT_CONFIG=True,
        CODEX_POLICY_HOOK_CONFIGURED=True,
    )
    settings.CODEX_HOME.mkdir(parents=True)
    (settings.CODEX_HOME / "config.toml").write_text(
        harness_config.replace("{script}", str(script))
    )
    workspace = settings.WORKSPACE_ROOT / "repo"
    (workspace / ".codex").mkdir(parents=True)
    (workspace / ".codex" / "config.toml").write_text(_server_table("wsmcp", script))
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)

    result = await CodexRuntime(settings=settings).execute_directive(
        DirectiveRequest(
            workspace_path=workspace,
            prompt="say hi",
            base_branch="main",
            work_branch="agent/work",
            mcp_servers=mcp_servers,
            extra_env=(("HTTPS_PROXY", _UNREACHABLE_PROXY), ("HTTP_PROXY", _UNREACHABLE_PROXY)),
            # A Codex api_key Directive with no proxy endpoint is refused before spawn (LA-04);
            # this test is about MCP isolation, so it runs the mode that reaches the child.
            auth_mode=AuthMode.SUBSCRIPTION,
        )
    )
    assert "thread.started" in result.stdout, result.stderr or result.error


_HARNESS_SERVER = _server_table("usermcp", Path("{script}"))


@pytest.mark.asyncio
async def test_no_planted_server_starts_when_no_server_is_bound(tmp_path: Path) -> None:
    # LA-19b: a Product with no bound server is locked too; both planted layers were live
    # before, so this also fails if Codex ever stops honouring the flags.
    await _run_directive(tmp_path, harness_config=_HARNESS_SERVER, mcp_servers=None)

    assert _started(tmp_path) == set()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "harness_config",
    [
        pytest.param(_HARNESS_SERVER, id="harness-root-server"),
        # A config.toml that does not parse made 0.141.0 load the Workspace layer again
        # under --ignore-user-config; an earlier Directive of the same Contract can write one.
        pytest.param(_HARNESS_SERVER + "this is = = not toml\n", id="unparseable-harness-root"),
    ],
)
async def test_only_the_runners_servers_start(tmp_path: Path, harness_config: str) -> None:
    runner_server = McpServerEntry(
        slug="runnermcp", command=str(tmp_path / "mcp-marker.sh"), args=("runnermcp",)
    )

    await _run_directive(tmp_path, harness_config=harness_config, mcp_servers=(runner_server,))

    assert _started(tmp_path) == {"runnermcp"}
