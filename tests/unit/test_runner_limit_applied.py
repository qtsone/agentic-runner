"""The Runner Limit as a Runner reads, applies and reports it (ADR-0022 §52-§58, ticket 02).

The contracts half -- the model, the wire field, the evaluator's ``runner_limit`` link -- is
``test_runner_limit.py``'s. Here: start refuses a limit it cannot apply; the tools part
reaches the verb seam, the Callback Socket and MCP planning; the networks part reaches the
egress proxy and a Directive's start; the folders part reaches the command policy and the
ACP permission seam, with the strict matching a deny-list needs; and registration and the
heartbeat carry the limit and its posture. An empty limit is the rest of the suite.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import shutil
import sys
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from temporalio.exceptions import ApplicationError

from agentic_runner import config as runner_config
from agentic_runner.activities import RunnerRalphActivities, _RuntimeContextState
from agentic_runner.callback import CALLBACK_TOKEN_ENV, AttemptCallbackServer, call
from agentic_runner.egress import EGRESS_REFUSED_SOURCE, EgressProxy, destination_denied
from agentic_runner.mcp import McpPlan, plan_mcp
from agentic_runner.runner_limit import (
    RUNNER_LIMIT_EVIDENCE_SOURCE,
    AppliedRunnerLimit,
    DeniedFolders,
    RunnerLimitError,
)
from agentic_runner.service import build_agent_runtimes, runner_limit_report
from agentic_runner.workers.acp_runtime import AcpRuntime
from agentic_runner.workers.agent_runtime import AuthMode, DirectiveRequest, PermissionFallback
from agentic_runner.workers.command_policy import CommandPolicy, evaluate_command_policy
from agentic_runner.workers.contract_isolation import DirectiveSandbox
from agentic_runner.workers.settings import WorkerSettings
from agentic_runner_contracts.grants import Decision, GrantSnapshot
from agentic_runner_contracts.runner_limit import RunnerLimit
from agentic_runner_contracts.runner_registration import (
    CliVersion,
    EgressPosture,
    FolderPosture,
)
from agentic_runner_contracts.runtime_context import McpServerSpec

REPOSITORY = "qts/agentic-os"
WORK_RECORD_ID = "123e4567-e89b-12d3-a456-426614174000"
AGENT_ID = "8f14e45f-ceea-467a-9a37-1a2b3c4d5e6f"
CONTRACT_ID = "1d0f6b8c-2e3f-4a5b-8c7d-9e0f1a2b3c4d"
MERGE_DENIED = {"resource_type": "repo", "selector": "*", "verbs": {"pr.merge": "deny"}}
FAKE_AGENT = Path(__file__).parents[1] / "fixtures" / "fake_acp_agent.py"


def _case_sensitive(_: Path) -> bool:
    return False


def _applied(
    tmp_path: Path, case_insensitive: Callable[[Path], bool] = _case_sensitive, **parts: Any
) -> AppliedRunnerLimit:
    return AppliedRunnerLimit.from_config(
        RunnerLimit.model_validate(parts),
        workspace_roots=(tmp_path / "workspaces",),
        home=str(tmp_path / "home"),
        case_insensitive=case_insensitive,
    )


class _FakeClient:
    def __init__(self) -> None:
        self.evidence: list[tuple[str, dict[str, Any]]] = []

    async def append_evidence(
        self,
        work_record_id: str,
        *,
        source: str,
        payload: Mapping[str, Any],
        actor: str = "temporal-worker",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        self.evidence.append((source, dict(payload)))
        return {"ok": True}

    def evaluations(self) -> list[dict[str, Any]]:
        return [payload for source, payload in self.evidence if source == "ralph.grant_evaluation"]


def _grant(**verbs: str) -> dict[str, Any]:
    return {"entries": [{"resource_type": "repo", "selector": REPOSITORY, "verbs": dict(verbs)}]}


def _state(**overrides: Any) -> _RuntimeContextState:
    snapshot = GrantSnapshot.from_payload(
        {
            "agent_id": AGENT_ID,
            "contract_id": CONTRACT_ID,
            "contract_state": "active",
            "dispatchable": True,
            "root_grant": _grant(**{"pr.merge": "allow", "push": "allow"}),
            "agent_grant": _grant(**{"pr.merge": "allow", "push": "allow"}),
            "persona_allow_list": _grant(**{"pr.merge": "allow", "push": "allow"}),
            "resources": [
                {"resource_type": "repo", "selector": REPOSITORY, "in_contract_scope": True}
            ],
        }
    )
    values: dict[str, Any] = {
        "reviewer": None,
        "repository": REPOSITORY,
        "verifier_argv": ("true",),
        "work_branch": "agent/work",
        "completion_criteria": "ship it",
        "agent_id": AGENT_ID,
        "grant_snapshot": snapshot,
    }
    values.update(overrides)
    return _RuntimeContextState(**values)


# ------------------------------------------------------------------ where it is set


def test_the_config_file_limits_table_is_read_and_a_home_folder_expanded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    path = tmp_path / "config.toml"
    path.write_text(
        '[limits]\nnetworks = ["evil.example"]\nfolders = ["~/clients", "$HOME/taxes"]\n'
        "[[limits.tools]]\n"
        'resource_type = "repo"\nselector = "*"\nverbs = { "pr.merge" = "deny" }\n'
    )

    limits = runner_config.load(config_file=path, environ={}).limits

    assert limits.networks == ("evil.example",)
    assert limits.folders == (str(tmp_path / "home" / "clients"), str(tmp_path / "home" / "taxes"))
    assert limits.tools[0].verbs == {"pr.merge": Decision.DENY}


def test_the_env_limit_is_json_as_the_chart_renders_it(tmp_path: Path) -> None:
    environ = {"AGENTIC_RUNNER_LIMITS": json.dumps({"folders": ["/srv/clients"]})}

    assert runner_config.load(config_file=tmp_path / "none.toml", environ=environ).limits == (
        RunnerLimit(folders=("/srv/clients",))
    )


@pytest.mark.parametrize(
    "environ",
    [
        {"AGENTIC_RUNNER_LIMITS": "folders=/srv"},
        {"AGENTIC_RUNNER_LIMITS": json.dumps({"folder": ["/srv"]})},
        {"AGENTIC_RUNNER_LIMITS": json.dumps({"folders": ["relative/path"]})},
        {
            "AGENTIC_RUNNER_LIMITS": json.dumps(
                {"tools": [{**MERGE_DENIED, "verbs": {"*": "allow"}}]}
            )
        },
    ],
)
def test_an_env_limit_that_does_not_parse_never_becomes_an_empty_one(
    tmp_path: Path, environ: dict[str, str]
) -> None:
    with pytest.raises((ValidationError, ValueError)):
        runner_config.load(config_file=tmp_path / "none.toml", environ=environ)


@pytest.mark.parametrize(
    "table",
    ['[limits]\nfolders = "/srv"\n', '[limits]\nnetwork = ["x"]\n', "[limits\nfolders = []\n"],
)
def test_a_file_limit_that_does_not_parse_never_becomes_an_empty_one(
    tmp_path: Path, table: str
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(table)

    with pytest.raises(ValueError):
        runner_config.load(config_file=path, environ={})


# ------------------------------------------------------------------ start refuses


@pytest.mark.parametrize(
    ("parts", "named"),
    [
        ({"tools": [{**MERGE_DENIED, "verbs": {"pr.squash": "deny"}}]}, "tools[0]"),
        ({"tools": [{**MERGE_DENIED, "resource_type": "wiki"}]}, "tools[0]"),
        ({"networks": ["github.com", "github.com:ssh"]}, "networks[1]"),
    ],
)
def test_start_refuses_an_entry_it_cannot_apply_naming_it(
    tmp_path: Path, parts: dict[str, Any], named: str
) -> None:
    with pytest.raises(RunnerLimitError, match=rf"Runner Limit {named.replace('[', r'\[')}"):
        _applied(tmp_path, **parts)


def test_start_accepts_a_wildcard_verb_and_an_mcp_servers_own_tool_names(tmp_path: Path) -> None:
    applied = _applied(
        tmp_path,
        tools=[
            {**MERGE_DENIED, "verbs": {"pr.*": "deny"}},
            {"resource_type": "mcp:linear", "selector": "*", "verbs": {"create_issue": "deny"}},
        ],
    )

    assert applied.tools_grant is not None


@pytest.mark.parametrize("folder", ["", "workspaces"])
def test_start_refuses_a_folder_that_holds_the_workspace_root(tmp_path: Path, folder: str) -> None:
    with pytest.raises(RunnerLimitError, match=r"folders\[0\] contains the Workspace root"):
        _applied(tmp_path, folders=[str(tmp_path / folder)])


def test_a_folder_inside_the_workspace_root_is_a_limit_not_a_refusal(tmp_path: Path) -> None:
    assert _applied(tmp_path, folders=[str(tmp_path / "workspaces" / "c1" / "secrets")])


# ------------------------------------------------------------------ tools


@pytest.mark.asyncio
async def test_a_denied_verb_at_the_seam_names_the_runner_limit_in_evidence(
    tmp_path: Path,
) -> None:
    client = _FakeClient()
    activities = RunnerRalphActivities(
        client,  # type: ignore[arg-type]
        runner_limit=_applied(tmp_path, tools=[MERGE_DENIED]),
    )

    verdict = await activities._authorize(
        WORK_RECORD_ID, _state(), verb="pr.merge", repository=REPOSITORY
    )
    allowed = await activities._authorize(
        WORK_RECORD_ID, _state(), verb="push", repository=REPOSITORY
    )

    assert verdict.decision.decision is Decision.DENY
    assert allowed.decision.decision is Decision.ALLOW
    merge, push = client.evaluations()
    assert merge["deciding_link"] == "runner_limit"
    assert push["deciding_link"] != "runner_limit"


@pytest.mark.asyncio
async def test_a_denied_verb_through_the_callback_socket_names_the_runner_limit(
    tmp_path: Path,
) -> None:
    client = _FakeClient()
    activities = RunnerRalphActivities(
        client,  # type: ignore[arg-type]
        runner_limit=_applied(tmp_path, tools=[MERGE_DENIED]),
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    # `sun_path` is ~104 bytes; pytest's `tmp_path` is too long to bind a socket under.
    socket_dir = Path(tempfile.mkdtemp(dir="/tmp"))
    server = AttemptCallbackServer(
        socket_path=socket_dir / "s.sock",
        handlers=activities._callback_handlers(WORK_RECORD_ID, _state(), workspace),
    )

    try:
        async with server:
            answer = await asyncio.to_thread(
                call,
                socket_path=server.socket_path,
                token=server.token,
                path="/v0/verb",
                payload={"verb": "pr.merge", "resource": REPOSITORY},
            )
    finally:
        shutil.rmtree(socket_dir, ignore_errors=True)

    assert answer["decision"] == "deny"
    assert answer["allowed"] is False
    (evaluation,) = client.evaluations()
    assert evaluation["deciding_link"] == "runner_limit"


def test_a_denied_mcp_server_is_withheld_never_started(tmp_path: Path) -> None:
    limit = _applied(
        tmp_path, tools=[{"resource_type": "mcp:linear", "selector": "*", "verbs": {"*": "deny"}}]
    )
    snapshot = GrantSnapshot.from_payload(
        {
            "agent_id": AGENT_ID,
            "contract_id": CONTRACT_ID,
            "contract_state": "active",
            "dispatchable": True,
            **{
                link: {
                    "entries": [
                        {"resource_type": "mcp:linear", "selector": "*", "verbs": {"*": "allow"}}
                    ]
                }
                for link in ("root_grant", "agent_grant", "persona_allow_list")
            },
            "resources": [
                {"resource_type": "mcp:linear", "selector": "linear", "in_contract_scope": True}
            ],
        }
    )
    spec = McpServerSpec(
        slug="linear",
        transport="stdio",
        config={"command": "/usr/bin/linear-mcp"},
        tools=["create_issue"],
    )

    plan = plan_mcp(snapshot, [spec], runner_limit=limit.tools_grant)
    unlimited = plan_mcp(snapshot, [spec])

    assert plan.granted == ()
    ((_, decision, why),) = plan.withheld
    assert why == "denied"
    assert decision.deciding.deciding_link == "runner_limit"
    assert len(unlimited.granted) == 1


# ------------------------------------------------------------------ networks


async def _proxied_status(proxy: EgressProxy, url: str) -> int:
    """One absolute-form request through ``proxy``; the status line's code."""

    port = int(proxy.env()["HTTP_PROXY"].rsplit(":", 1)[1])
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    credential = base64.b64encode(b"agentic:attempt-bearer").decode()
    writer.write(
        f"GET {url} HTTP/1.1\r\nHost: x\r\nProxy-Authorization: Basic {credential}\r\n\r\n".encode()
    )
    await writer.drain()
    status = int((await reader.readline()).split()[1])
    writer.close()
    return status


@pytest.mark.asyncio
async def test_a_destination_the_profile_allows_and_the_limit_denies_is_refused() -> None:
    refused: list[tuple[str, int, int | None]] = []

    async def on_refused(host: str, port: int, entry: int | None) -> None:
        refused.append((host, port, entry))

    async def resolver(host: str, port: int) -> tuple[str, int]:
        raise OSError("this test reaches no origin")

    async with EgressProxy(
        ["*.example.com"],
        token="attempt-bearer",
        deny_list=["other.example", "denied.example.com"],
        resolver=resolver,
        on_refused=on_refused,
    ) as proxy:
        denied = await _proxied_status(proxy, "http://denied.example.com/")
        # Allowed and not denied: on to the (failing) resolver, so a 502 and not a 403.
        passed = await _proxied_status(proxy, "http://ok.example.com/")

    assert denied == 403
    assert passed == 502
    assert refused == [("denied.example.com", 80, 1)]


@pytest.mark.asyncio
async def test_with_no_profile_allow_list_the_limit_alone_refuses() -> None:
    async def resolver(host: str, port: int) -> tuple[str, int]:
        raise OSError("this test reaches no origin")

    async with EgressProxy(
        None, token="attempt-bearer", deny_list=["denied.example"], resolver=resolver
    ) as proxy:
        assert await _proxied_status(proxy, "http://denied.example/") == 403
        assert await _proxied_status(proxy, "http://anything.example/") == 502


def test_a_deny_entry_is_compared_whatever_its_case() -> None:
    assert destination_denied(["GitHub.COM"], "github.com", 443) == 0
    assert destination_denied(["github.com"], "GITHUB.com", 443) == 0


def test_a_deny_entry_is_compared_whatever_its_trailing_dot() -> None:
    assert destination_denied(["github.com."], "github.com", 443) == 0
    assert destination_denied(["github.com"], "github.com.", 443) == 0


def test_a_deny_entry_with_a_port_denies_that_port_only() -> None:
    assert destination_denied(["github.com:22"], "github.com", 22) == 0
    assert destination_denied(["github.com:22"], "github.com", 443) is None
    assert destination_denied(["github.com"], "github.com", 8443) == 0


def test_a_deny_entry_matches_an_ip_literal_however_it_is_spelled() -> None:
    assert destination_denied(["[::1]:443"], "0:0:0:0:0:0:0:1", 443) == 0
    assert destination_denied(["::1"], "[::1]", 80) == 0
    assert destination_denied(["127.0.0.1"], "::ffff:127.0.0.1", 80) == 0
    # A hostname entry denies the name only, not the host's addresses (ADR-0022 §54).
    assert destination_denied(["localhost"], "127.0.0.1", 80) is None


@pytest.mark.asyncio
async def test_a_directive_whose_git_remote_is_denied_fails_at_start_naming_the_entry(
    tmp_path: Path,
) -> None:
    client = _FakeClient()
    activities = RunnerRalphActivities(
        client,  # type: ignore[arg-type]
        runner_limit=_applied(tmp_path, networks=["gitlab.com", "github.com"]),
    )
    env = {CALLBACK_TOKEN_ENV: "attempt-bearer"}

    with pytest.raises(ApplicationError, match=r"networks\[1\] denies github.com") as refusal:
        async with contextlib.AsyncExitStack() as stack:
            await activities._start_mcp_and_egress(
                stack,
                work_record_id=WORK_RECORD_ID,
                state=_state(),
                plan=McpPlan(),
                credentials=None,
                env=env,
            )

    assert refusal.value.non_retryable
    [(source, payload)] = client.evidence
    assert source == RUNNER_LIMIT_EVIDENCE_SOURCE
    assert payload["entry"] == "networks[1]"
    assert payload["needed_for"] == "git remote"


@pytest.mark.asyncio
async def test_a_network_limit_puts_the_directive_behind_the_proxy_without_opening_codex(
    tmp_path: Path,
) -> None:
    client = _FakeClient()
    activities = RunnerRalphActivities(
        client,  # type: ignore[arg-type]
        runner_limit=_applied(tmp_path, networks=["evil.example"]),
    )
    env: dict[str, str] = {}

    async with contextlib.AsyncExitStack() as stack:
        _, allow_list = await activities._start_mcp_and_egress(
            stack,
            work_record_id=WORK_RECORD_ID,
            state=_state(),
            plan=McpPlan(),
            credentials=None,
            env=env,
        )

    # No Profile list: Codex opens the network only on one, so the list stays empty.
    assert allow_list == ()
    assert env["HTTPS_PROXY"].startswith("http://agentic:")
    assert not [source for source, _ in client.evidence if source == EGRESS_REFUSED_SOURCE]


# ------------------------------------------------------------------ folders


def _folders(tmp_path: Path, *entries: Path | str, case_insensitive: bool = False) -> DeniedFolders:
    return DeniedFolders(
        [str(entry) for entry in entries],
        home=str(tmp_path / "home"),
        case_insensitive=lambda _: case_insensitive,
    )


def test_a_command_naming_a_denied_folder_is_refused_by_position_only(tmp_path: Path) -> None:
    workspace = tmp_path / "workspaces" / "c1" / "wr1"
    workspace.mkdir(parents=True)
    denied = tmp_path / "clients" / "acme"

    decision = evaluate_command_policy(
        argv=["pytest", str(denied / "tests")],
        cwd=workspace,
        workspace_root=tmp_path / "workspaces",
        base_branch="main",
        work_branch="agent/wr1",
        policy=CommandPolicy(),
        denied_folders=_folders(tmp_path, tmp_path / "elsewhere", denied),
    )

    assert decision.allowed is False
    assert decision.reason == "this Runner's Runner Limit denies folders[1]"
    assert str(tmp_path) not in decision.reason


def test_a_symlink_into_a_denied_folder_is_refused(tmp_path: Path) -> None:
    denied = tmp_path / "clients"
    denied.mkdir()
    (tmp_path / "link").symlink_to(denied)
    folders = _folders(tmp_path, denied)

    assert folders.refusal([str(tmp_path / "link" / "a.txt")], tmp_path) == "folders[0]"


def test_a_symlinked_entry_denies_its_target_too(tmp_path: Path) -> None:
    target = tmp_path / "real"
    target.mkdir()
    (tmp_path / "alias").symlink_to(target)
    folders = _folders(tmp_path, tmp_path / "alias")

    assert folders.refusal([str(target / "a.txt")], tmp_path) == "folders[0]"
    assert folders.refusal([str(tmp_path / "alias" / "a.txt")], tmp_path) == "folders[0]"


def test_dot_dot_cannot_climb_into_a_denied_folder(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    folders = _folders(tmp_path, tmp_path / "clients")

    assert folders.refusal(["../clients/acme.txt"], workspace) == "folders[0]"
    assert folders.refusal([f"{workspace}/../clients"], workspace) == "folders[0]"


def test_a_folder_denies_whole_components_only(tmp_path: Path) -> None:
    folders = _folders(tmp_path, tmp_path / "a" / "b")

    assert folders.refusal([str(tmp_path / "a" / "b" / "c")], tmp_path) == "folders[0]"
    assert folders.refusal([str(tmp_path / "a" / "b")], tmp_path) == "folders[0]"
    assert folders.refusal([str(tmp_path / "a" / "bc")], tmp_path) is None


def test_case_is_folded_only_on_a_case_insensitive_filesystem(tmp_path: Path) -> None:
    other_case = str(tmp_path / "Clients" / "ACME")
    folding = _folders(tmp_path, tmp_path / "clients", case_insensitive=True)

    assert folding.refusal([other_case], tmp_path) == "folders[0]"
    assert _folders(tmp_path, tmp_path / "clients").refusal([other_case], tmp_path) is None


def test_tilde_and_home_are_expanded_in_an_argument(tmp_path: Path) -> None:
    folders = _folders(tmp_path, tmp_path / "home" / "clients")

    assert folders.refusal(["~/clients/acme"], tmp_path) == "folders[0]"
    assert folders.refusal(["$HOME/clients"], tmp_path) == "folders[0]"
    assert folders.refusal(["${HOME}/clients/x"], tmp_path) == "folders[0]"
    assert folders.refusal(["--rootdir=~/clients"], tmp_path) == "folders[0]"


def test_an_argument_with_an_unexpanded_variable_is_refused(tmp_path: Path) -> None:
    folders = _folders(tmp_path, tmp_path / "clients")

    assert folders.refusal(["$CLIENTS/acme"], tmp_path) == "folders[*]"
    assert folders.refusal(["${PROJECT_DIR}"], tmp_path) == "folders[*]"


def test_a_glob_that_could_reach_a_denied_folder_is_refused(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    folders = _folders(tmp_path, tmp_path / "clients" / "acme")

    assert folders.refusal([f"{tmp_path}/*/acme/notes.txt"], workspace) == "folders[0]"
    assert folders.refusal([f"{tmp_path}/clients/a*"], workspace) == "folders[0]"
    assert folders.refusal(["../clients/**"], workspace) == "folders[0]"
    # A glob rooted beside the folder cannot reach it.
    assert folders.refusal(["tests/*.py"], workspace) is None
    assert folders.refusal([f"{tmp_path}/other/*"], workspace) is None


def test_an_empty_limit_refuses_no_word(tmp_path: Path) -> None:
    assert DeniedFolders(()).refusal(["$ANYTHING", "/*", "~"], tmp_path) is None


# ------------------------------------------------------------------ the ACP permission seam


@pytest.fixture
def _no_rlimits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(DirectiveSandbox, "spawn_kwargs", lambda self: {})


def _acp(tmp_path: Path, cli_kind: str, folders: DeniedFolders) -> AcpRuntime:
    return AcpRuntime(
        cli_kind=cli_kind,
        settings=WorkerSettings(
            TEMPORAL_ADDRESS="127.0.0.1:7233",
            INTERNAL_FASTAPI_BASE_URL="http://agentic-api.internal:8000",
            WORKSPACE_ROOT=tmp_path / "workspaces",
            CODEX_HOME=tmp_path / "codex-home",
            CODEX_CLI_TIMEOUT_SECONDS=30,
            CLAUDE_CLI_TIMEOUT_SECONDS=30,
        ),
        bridge_argv=[sys.executable, str(FAKE_AGENT)],
        denied_folders=folders,
    )


def _acp_request(tmp_path: Path, scenario: dict[str, Any], cli_kind: str) -> DirectiveRequest:
    record = tmp_path / "record.jsonl"
    scenario_path = tmp_path / "scenario.json"
    scenario_path.write_text(json.dumps({"record": str(record), **scenario}))
    workspace = tmp_path / "workspaces" / "c1" / "wr1"
    workspace.mkdir(parents=True, exist_ok=True)
    home = tmp_path / "contract" / "home"
    (home / "tmp").mkdir(parents=True, exist_ok=True)
    harness = tmp_path / "contract" / "harness"
    harness.mkdir(parents=True, exist_ok=True)
    proxy = {"ANTHROPIC_BASE_URL": "http://127.0.0.1:4000/a", "ANTHROPIC_AUTH_TOKEN": "b"}
    if cli_kind == "codex_cli":
        proxy = {"OPENAI_BASE_URL": "http://127.0.0.1:4000/a/v1", "OPENAI_API_KEY": "b"}
    return DirectiveRequest(
        workspace_path=workspace,
        prompt="Implement the change.",
        base_branch="main",
        work_branch="agent/wr1",
        sandbox=DirectiveSandbox(
            home_dir=home, harness_config_dir=harness, max_processes=64, max_memory_bytes=1 << 30
        ),
        extra_env=(("FAKE_ACP_SCENARIO", str(scenario_path)), *proxy.items()),
        auth_mode=AuthMode.API_KEY,
        permission_fallback=PermissionFallback.HOLD,
    )


def _received(tmp_path: Path, method: str | None = None) -> list[dict[str, Any]]:
    entries = [json.loads(line) for line in (tmp_path / "record.jsonl").read_text().splitlines()]
    messages = [entry["received"] for entry in entries if "received" in entry]
    return [m for m in messages if method is None or m.get("method") == method]


@pytest.mark.asyncio
@pytest.mark.usefixtures("_no_rlimits")
@pytest.mark.parametrize(
    "tool_call",
    [
        {"title": "cat", "rawInput": {"command": "cat {denied}/contract.pdf"}},
        {"title": "sh", "rawInput": {"command": ["/bin/zsh", "-lc", "ls {denied} | wc -l"]}},
        {"title": "Read", "rawInput": {"file_path": "{denied}/contract.pdf"}},
        {"title": "Read", "locations": [{"path": "{denied}/contract.pdf"}]},
    ],
)
async def test_an_acp_permission_request_for_a_denied_folder_is_refused_by_position_only(
    tmp_path: Path, tool_call: dict[str, Any]
) -> None:
    denied = tmp_path / "clients"
    request = json.loads(json.dumps(tool_call).replace("{denied}", str(denied)))

    result = await _acp(tmp_path, "codex_cli", _folders(tmp_path, denied)).execute_directive(
        _acp_request(tmp_path, {"permissions": [request], "stop_reason": "cancelled"}, "codex_cli")
    )

    answers = [m["result"]["outcome"] for m in _received(tmp_path) if "method" not in m]
    assert answers == [{"outcome": "selected", "optionId": "cancel"}]
    notes = " ".join(result.evidence.notes)
    assert "permission denied: this Runner's Runner Limit denies folders[0]" in notes
    # Held for nobody: a denied folder is no question for a person.
    assert "held" not in notes
    assert str(tmp_path) not in notes
    assert "contract.pdf" not in notes


@pytest.mark.asyncio
@pytest.mark.usefixtures("_no_rlimits")
async def test_claude_code_gets_the_folders_as_its_own_deny_rules(tmp_path: Path) -> None:
    denied = tmp_path / "clients"

    await _acp(tmp_path, "claude_code", _folders(tmp_path, denied)).execute_directive(
        _acp_request(tmp_path, {}, "claude_code")
    )

    (new,) = _received(tmp_path, "session/new")
    deny = new["params"]["_meta"]["claudeCode"]["options"]["settings"]["permissions"]["deny"]
    resolved = Path(os.path.realpath(denied)).as_posix()
    assert f"Read(/{resolved}/**)" in deny
    assert f"Edit(/{resolved}/**)" in deny


@pytest.mark.asyncio
@pytest.mark.usefixtures("_no_rlimits")
async def test_an_empty_limit_sends_claude_code_no_settings(tmp_path: Path) -> None:
    await _acp(tmp_path, "claude_code", DeniedFolders(())).execute_directive(
        _acp_request(tmp_path, {}, "claude_code")
    )

    (new,) = _received(tmp_path, "session/new")
    assert "settings" not in new["params"]["_meta"]["claudeCode"]["options"]


# ------------------------------------------------------------------ posture and report


def test_posture_is_enforced_tools_the_egress_posture_and_folders_per_harness(
    tmp_path: Path,
) -> None:
    settings = WorkerSettings(
        TEMPORAL_ADDRESS="127.0.0.1:7233",
        INTERNAL_FASTAPI_BASE_URL="http://agentic-api.internal:8000",
        WORKSPACE_ROOT=tmp_path / "workspaces",
        ACP_CLI_KINDS="claude_code",
    )
    clis = [
        CliVersion(cli_kind="claude_code", version="2.1.280", meets_floor=True),
        CliVersion(cli_kind="codex_cli", version="0.130.0", meets_floor=True),
    ]
    limit = _applied(tmp_path, folders=[str(tmp_path / "clients")])

    report = runner_limit_report(
        limit,
        build_agent_runtimes(settings, clis, denied_folders=limit.folders),
        egress_posture=EgressPosture.UNENFORCED,
    )

    assert report is not None
    assert report.posture.tools == "enforced"
    assert report.posture.networks is EgressPosture.UNENFORCED
    assert report.posture.folders == {
        "claude_code": FolderPosture.CHECKED,
        "codex_cli": FolderPosture.UNENFORCED,
    }
    assert report.limit.folders == (str(tmp_path / "clients"),)


def test_an_empty_limit_reports_nothing_so_an_older_control_plane_keeps_the_heartbeat(
    tmp_path: Path,
) -> None:
    assert _applied(tmp_path).report(egress_posture=EgressPosture.UNRESTRICTED, folders={}) is None


def test_a_path_with_a_nul_byte_is_refused_rather_than_raising(tmp_path: Path) -> None:
    assert _folders(tmp_path, tmp_path / "clients").refusal(["/etc\x00/x"], tmp_path) == (
        "folders[*]"
    )


@pytest.mark.asyncio
@pytest.mark.usefixtures("_no_rlimits")
async def test_a_codex_patch_into_a_denied_folder_is_refused(tmp_path: Path) -> None:
    denied = tmp_path / "clients"
    patch = {"title": "Edit", "rawInput": {"changes": {str(denied / "a.txt"): {"add": "x"}}}}

    result = await _acp(tmp_path, "codex_cli", _folders(tmp_path, denied)).execute_directive(
        _acp_request(tmp_path, {"permissions": [patch], "stop_reason": "cancelled"}, "codex_cli")
    )

    assert "Runner Limit denies folders[0]" in " ".join(result.evidence.notes)
