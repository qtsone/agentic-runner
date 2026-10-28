"""A Directive's Guardrails as the Runner enforces them (Spec: Agent Guardrails rev 7).

The contract half -- the model, the posture on the heartbeat, the 3.x bodies -- is
``test_guardrails_contract.py``'s. Here: the commands part reaches the command policy and
the ACP permission seam; the network part reaches the egress proxy and a Directive's start;
the folders part reaches both, with the strict matching a deny-list needs; the limits
reach the turn and the sandbox; every refusal writes Evidence naming the layer that
refused; and a Runner Limit left in the config is ignored with one warning.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from temporalio.exceptions import ApplicationError

from agentic_runner import config as runner_config
from agentic_runner.activities import RunnerRalphActivities, _RuntimeContextState
from agentic_runner.callback import CALLBACK_TOKEN_ENV
from agentic_runner.egress import EgressProxy, destination_denied
from agentic_runner.guardrails import (
    ANY_FOLDER,
    DeniedFolders,
    DirectiveGuardrails,
    GuardrailRefusal,
    turn_timeout,
)
from agentic_runner.mcp import McpPlan
from agentic_runner.service import build_agent_runtimes, guardrails_posture
from agentic_runner.workers.acp_runtime import AcpRuntime
from agentic_runner.workers.agent_runtime import (
    AuthMode,
    DirectiveEvidence,
    DirectiveRequest,
    DirectiveResult,
    PermissionFallback,
)
from agentic_runner.workers.codex_runtime import CodexRuntime
from agentic_runner.workers.command_policy import (
    NOT_ALLOWLISTED,
    CommandPolicy,
    evaluate_command_policy,
)
from agentic_runner.workers.contract_isolation import DirectiveSandbox
from agentic_runner.workers.settings import WorkerSettings
from agentic_runner_contracts.grants import GrantSnapshot
from agentic_runner_contracts.guardrails import (
    GUARDRAILS_EVIDENCE_SOURCE,
    GuardrailLayer,
    GuardrailPart,
    Guardrails,
)
from agentic_runner_contracts.runner_registration import (
    CliVersion,
    EgressPosture,
    GuardrailPosture,
)

REPOSITORY = "qts/agentic-os"
WORK_RECORD_ID = "123e4567-e89b-12d3-a456-426614174000"
AGENT_ID = "8f14e45f-ceea-467a-9a37-1a2b3c4d5e6f"
CONTRACT_ID = "1d0f6b8c-2e3f-4a5b-8c7d-9e0f1a2b3c4d"
FAKE_AGENT = Path(__file__).parents[1] / "fixtures" / "fake_acp_agent.py"


def _guardrails(**fields: Any) -> Guardrails:
    return Guardrails.model_validate(fields)


def _removed(part: str, entry: str, layer: str) -> dict[str, str]:
    return {"part": part, "entry": entry, "layer": layer}


def _directive(
    tmp_path: Path, guardrails: Guardrails, *, case_insensitive: bool = False
) -> DirectiveGuardrails:
    return DirectiveGuardrails(
        guardrails,
        workspace=tmp_path / "workspace",
        home=str(tmp_path / "home"),
        case_insensitive=lambda _: case_insensitive,
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

    def refusals(self) -> list[dict[str, Any]]:
        return [
            payload for source, payload in self.evidence if source == GUARDRAILS_EVIDENCE_SOURCE
        ]


def _state(guardrails: Guardrails, **overrides: Any) -> _RuntimeContextState:
    grant = {"entries": [{"resource_type": "repo", "selector": REPOSITORY, "verbs": {}}]}
    snapshot = GrantSnapshot.from_payload(
        {
            "agent_id": AGENT_ID,
            "contract_id": CONTRACT_ID,
            "contract_state": "active",
            "dispatchable": True,
            "root_grant": grant,
            "agent_grant": grant,
            "persona_allow_list": grant,
            "resources": [],
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
        "guardrails": guardrails,
    }
    values.update(overrides)
    return _RuntimeContextState(**values)


# ------------------------------------------------------------------ a leftover Runner Limit


def test_a_leftover_runner_limit_file_is_ignored_with_one_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "config.toml"
    path.write_text('log_level = "debug"\n[limits]\nfolders = ["~/clients"]\n')
    environ = {"AGENTIC_RUNNER_LIMITS": json.dumps({"networks": ["evil.example"]})}

    with caplog.at_level(logging.WARNING, logger=runner_config.__name__):
        loaded = runner_config.load(config_file=path, environ=environ)

    assert loaded.log_level == "debug"
    assert not hasattr(loaded, "limits")
    assert [record.getMessage() for record in caplog.records] == [
        runner_config.RETIRED_LIMITS_WARNING
    ]


def test_a_config_with_no_runner_limit_warns_nothing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "config.toml"
    path.write_text('log_level = "debug"\n')

    with caplog.at_level(logging.WARNING, logger=runner_config.__name__):
        runner_config.load(config_file=path, environ={})

    assert caplog.records == []


# ------------------------------------------------------------------ commands


def _policy(tmp_path: Path, argv: list[str], guardrails: Guardrails) -> Any:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    return evaluate_command_policy(
        argv=argv,
        cwd=workspace,
        workspace_root=workspace,
        base_branch="main",
        work_branch="agent/wr1",
        policy=CommandPolicy(),
        guardrails=_directive(tmp_path, guardrails),
    )


def test_a_program_the_compiled_commands_omit_is_refused_naming_the_organisation(
    tmp_path: Path,
) -> None:
    guardrails = _guardrails(commands=["npm test", "pytest"])

    refused = _policy(tmp_path, ["make", "deploy"], guardrails)
    listed = _policy(tmp_path, ["npm", "test", "--", "-w"], guardrails)
    other_npm = _policy(tmp_path, ["npm", "publish"], guardrails)

    assert refused.allowed is False
    assert refused.reason == NOT_ALLOWLISTED
    assert refused.refusal == GuardrailRefusal(
        GuardrailPart.COMMANDS, "make", GuardrailLayer.ORGANISATION
    )
    assert listed.allowed is True
    assert other_npm.allowed is False


def test_a_command_a_narrowing_layer_removed_is_refused_naming_that_layer(tmp_path: Path) -> None:
    guardrails = _guardrails(commands=["pytest"], removed=[_removed("commands", "npm", "product")])

    decision = _policy(tmp_path, ["npm", "install"], guardrails)

    assert decision.allowed is False
    assert decision.refusal == GuardrailRefusal(
        GuardrailPart.COMMANDS, "npm", GuardrailLayer.PRODUCT
    )
    assert decision.reason == "Refused by the Product's Guardrails: commands `npm`"


def test_a_removal_from_the_built_in_policy_is_refused_too(tmp_path: Path) -> None:
    guardrails = _guardrails(removed=[_removed("commands", "pytest", "agent")])

    assert _policy(tmp_path, ["pytest", "tests"], guardrails).refusal is not None
    assert _policy(tmp_path, ["git", "status"], guardrails).allowed is True


def test_listing_a_command_never_lifts_the_hard_deny_rules(tmp_path: Path) -> None:
    guardrails = _guardrails(commands=["kubectl", "git"])

    assert _policy(tmp_path, ["kubectl", "get", "pods"], guardrails).allowed is False
    # A built-in program keeps the shape the policy holds it to.
    assert _policy(tmp_path, ["git", "push", "origin", "main"], guardrails).allowed is False
    assert _policy(tmp_path, ["git", "status"], guardrails).allowed is True


# ------------------------------------------------------------------ network


async def _proxied_status(env: Mapping[str, str], url: str) -> int:
    """One absolute-form request through the attempt's proxy; the status line's code."""

    port = int(env["HTTP_PROXY"].rsplit(":", 1)[1])
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    credential = base64.b64encode(f"agentic:{env[CALLBACK_TOKEN_ENV]}".encode()).decode()
    writer.write(
        f"GET {url} HTTP/1.1\r\nHost: x\r\nProxy-Authorization: Basic {credential}\r\n\r\n".encode()
    )
    await writer.drain()
    status = int((await reader.readline()).split()[1])
    writer.close()
    return status


async def _no_origin(host: str, port: int) -> tuple[str, int]:
    raise OSError("this test reaches no origin")


@pytest.mark.asyncio
async def test_a_destination_refused_by_a_network_rule_writes_evidence_naming_the_layer() -> None:
    client = _FakeClient()
    activities = RunnerRalphActivities(
        client,  # type: ignore[arg-type]
        egress_resolver=_no_origin,
    )
    guardrails = _guardrails(
        network=["*.example.com"],
        removed=[_removed("network", "db.prod.internal", "product")],
    )
    env: dict[str, str] = {}

    async with contextlib.AsyncExitStack() as stack:
        await activities._start_mcp_and_egress(
            stack,
            work_record_id=WORK_RECORD_ID,
            state=_state(guardrails),
            plan=McpPlan(),
            credentials=None,
            env=env,
        )
        removed = await _proxied_status(env, "http://db.prod.internal/")
        unlisted = await _proxied_status(env, "http://elsewhere.test/")
        listed = await _proxied_status(env, "http://api.example.com/")

    assert (removed, unlisted, listed) == (403, 403, 502)
    by_product, by_organisation = client.refusals()
    assert by_product["layer"] == "product"
    assert by_product["part"] == "network"
    assert by_product["entry"] == "db.prod.internal"
    assert (
        by_product["summary"] == "Refused by the Product's Guardrails: network `db.prod.internal`"
    )
    assert by_organisation["layer"] == "organisation"
    assert by_organisation["host"] == "elsewhere.test"


@pytest.mark.asyncio
async def test_with_no_allow_list_the_removed_destinations_alone_are_refused() -> None:
    async with EgressProxy(
        None, token="attempt-bearer", deny_list=["denied.example"], resolver=_no_origin
    ) as proxy:
        env = {**proxy.env(), CALLBACK_TOKEN_ENV: "attempt-bearer"}
        assert await _proxied_status(env, "http://denied.example/") == 403
        assert await _proxied_status(env, "http://anything.example/") == 502


@pytest.mark.asyncio
async def test_a_directive_whose_git_remote_was_removed_fails_at_start_naming_the_layer() -> None:
    client = _FakeClient()
    activities = RunnerRalphActivities(client)  # type: ignore[arg-type]
    guardrails = _guardrails(removed=[_removed("network", "github.com", "runner")])
    env = {CALLBACK_TOKEN_ENV: "attempt-bearer"}

    with pytest.raises(ApplicationError, match="Refused by the Runner's Guardrails") as refusal:
        async with contextlib.AsyncExitStack() as stack:
            await activities._start_mcp_and_egress(
                stack,
                work_record_id=WORK_RECORD_ID,
                state=_state(guardrails),
                plan=McpPlan(),
                credentials=None,
                env=env,
            )

    assert refusal.value.non_retryable
    [payload] = client.refusals()
    assert payload["layer"] == "runner"
    assert payload["needed_for"] == "git remote"


@pytest.mark.asyncio
async def test_a_removed_destination_puts_the_directive_behind_the_proxy() -> None:
    activities = RunnerRalphActivities(_FakeClient())  # type: ignore[arg-type]
    guardrails = _guardrails(removed=[_removed("network", "evil.example", "agent")])
    env: dict[str, str] = {}

    async with contextlib.AsyncExitStack() as stack:
        _, allow_list = await activities._start_mcp_and_egress(
            stack,
            work_record_id=WORK_RECORD_ID,
            state=_state(guardrails),
            plan=McpPlan(),
            credentials=None,
            env=env,
        )

    # No allow-list: Codex opens the network only on one, so the list stays empty.
    assert allow_list == ()
    assert env["HTTPS_PROXY"].startswith("http://agentic:")


def test_a_deny_entry_is_compared_whatever_its_case_and_trailing_dot() -> None:
    assert destination_denied(["GitHub.COM"], "github.com", 443) == 0
    assert destination_denied(["github.com."], "github.com", 443) == 0
    assert destination_denied(["github.com"], "github.com.", 443) == 0


def test_a_deny_entry_with_a_port_denies_that_port_only() -> None:
    assert destination_denied(["github.com:22"], "github.com", 22) == 0
    assert destination_denied(["github.com:22"], "github.com", 443) is None
    assert destination_denied(["github.com"], "github.com", 8443) == 0


@pytest.mark.parametrize(
    ("entry", "spelling"),
    [
        ("10.0.0.5", "167772165"),
        ("10.0.0.5", "0xa.0.0.5"),
        ("10.0.0.5", "10.5"),
        ("127.0.0.1", "0x7f000001"),
        ("127.0.0.1", "::ffff:127.0.0.1"),
        ("[::1]:443", "0:0:0:0:0:0:0:1"),
    ],
)
def test_a_deny_entry_matches_every_spelling_getaddrinfo_connects(
    entry: str, spelling: str
) -> None:
    assert destination_denied([entry], spelling, 443) == 0


def test_a_name_that_only_looks_numeric_is_left_a_name() -> None:
    assert destination_denied(["10.0.0.5"], "10.0.0.5.example.com", 443) is None
    # A hostname entry denies the name only, not the host's addresses.
    assert destination_denied(["localhost"], "127.0.0.1", 80) is None


# ------------------------------------------------------------------ folders


def _folders(tmp_path: Path, *entries: Path | str, case_insensitive: bool = False) -> DeniedFolders:
    return DeniedFolders(
        [str(entry) for entry in entries],
        home=str(tmp_path / "home"),
        case_insensitive=lambda _: case_insensitive,
    )


def test_a_command_naming_a_removed_folder_is_refused_naming_the_layer(tmp_path: Path) -> None:
    denied = tmp_path / "clients" / "acme"
    guardrails = _guardrails(
        removed=[
            _removed("folders", str(tmp_path / "elsewhere"), "product"),
            _removed("folders", str(tmp_path / "clients"), "runner"),
        ]
    )

    decision = _policy(tmp_path, ["pytest", str(denied / "tests")], guardrails)

    assert decision.allowed is False
    assert decision.refusal == GuardrailRefusal(
        GuardrailPart.FOLDERS, str(tmp_path / "clients"), GuardrailLayer.RUNNER
    )
    assert decision.reason.startswith("Refused by the Runner's Guardrails: folders")


def test_a_folder_removed_inside_the_workspace_is_read_from_its_root(tmp_path: Path) -> None:
    guardrails = _directive(tmp_path, _guardrails(removed=[_removed("folders", "secrets", "team")]))
    workspace = tmp_path / "workspace"

    assert guardrails.folder_refusal(["secrets/token.txt"], workspace) == GuardrailRefusal(
        GuardrailPart.FOLDERS, "secrets", GuardrailLayer.TEAM
    )
    assert guardrails.folder_refusal(["src/secrets.py"], workspace) is None


def test_a_path_outside_the_compiled_folders_is_refused_naming_the_organisation(
    tmp_path: Path,
) -> None:
    guardrails = _directive(tmp_path, _guardrails(folders=["src", "tests"]))
    workspace = tmp_path / "workspace"

    assert guardrails.folder_refusal(["cat", "infra/prod.tf"], workspace) == GuardrailRefusal(
        GuardrailPart.FOLDERS, "infra/prod.tf", GuardrailLayer.ORGANISATION
    )
    assert guardrails.folder_refusal(["pytest", "tests/unit", "-k", "x"], workspace) is None
    # Not plainly a path, and outside the Workspace: neither is the allow-list's to judge.
    assert guardrails.folder_refusal(["install", "/usr/lib/x"], workspace) is None


def test_a_symlink_into_a_denied_folder_is_refused(tmp_path: Path) -> None:
    denied = tmp_path / "clients"
    denied.mkdir()
    (tmp_path / "link").symlink_to(denied)

    assert _folders(tmp_path, denied).refusal([str(tmp_path / "link" / "a.txt")], tmp_path) == 0


def test_a_symlinked_entry_denies_its_target_too(tmp_path: Path) -> None:
    target = tmp_path / "real"
    target.mkdir()
    (tmp_path / "alias").symlink_to(target)
    folders = _folders(tmp_path, tmp_path / "alias")

    assert folders.refusal([str(target / "a.txt")], tmp_path) == 0
    assert folders.refusal([str(tmp_path / "alias" / "a.txt")], tmp_path) == 0


def test_dot_dot_cannot_climb_into_a_denied_folder(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    folders = _folders(tmp_path, tmp_path / "clients")

    assert folders.refusal(["../clients/acme.txt"], workspace) == 0
    assert folders.refusal([f"{workspace}/../clients"], workspace) == 0


def test_a_folder_denies_whole_components_only(tmp_path: Path) -> None:
    folders = _folders(tmp_path, tmp_path / "a" / "b")

    assert folders.refusal([str(tmp_path / "a" / "b" / "c")], tmp_path) == 0
    assert folders.refusal([str(tmp_path / "a" / "b")], tmp_path) == 0
    assert folders.refusal([str(tmp_path / "a" / "bc")], tmp_path) is None


def test_case_is_folded_only_on_a_case_insensitive_filesystem(tmp_path: Path) -> None:
    other_case = str(tmp_path / "Clients" / "ACME")

    assert (
        _folders(tmp_path, tmp_path / "clients", case_insensitive=True).refusal(
            [other_case], tmp_path
        )
        == 0
    )
    assert _folders(tmp_path, tmp_path / "clients").refusal([other_case], tmp_path) is None


def test_tilde_and_home_are_expanded_in_an_argument(tmp_path: Path) -> None:
    folders = _folders(tmp_path, tmp_path / "home" / "clients")

    assert folders.refusal(["~/clients/acme"], tmp_path) == 0
    assert folders.refusal(["$HOME/clients"], tmp_path) == 0
    assert folders.refusal(["${HOME}/clients/x"], tmp_path) == 0
    assert folders.refusal(["--rootdir=~/clients"], tmp_path) == 0


def test_an_argument_with_an_unexpanded_variable_or_a_nul_is_refused(tmp_path: Path) -> None:
    folders = _folders(tmp_path, tmp_path / "clients")

    assert folders.refusal(["$CLIENTS/acme"], tmp_path) == ANY_FOLDER
    assert folders.refusal(["${PROJECT_DIR}"], tmp_path) == ANY_FOLDER
    assert folders.refusal(["/etc\x00/x"], tmp_path) == ANY_FOLDER


def test_a_glob_that_could_reach_a_denied_folder_is_refused(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    folders = _folders(tmp_path, tmp_path / "clients" / "acme")

    assert folders.refusal([f"{tmp_path}/*/acme/notes.txt"], workspace) == 0
    assert folders.refusal([f"{tmp_path}/clients/a*"], workspace) == 0
    assert folders.refusal(["../clients/**"], workspace) == 0
    # A glob rooted beside the folder cannot reach it.
    assert folders.refusal(["tests/*.py"], workspace) is None
    assert folders.refusal([f"{tmp_path}/other/*"], workspace) is None


@pytest.mark.parametrize(
    "word",
    ["@{d}/x", "{d}:/mnt", "-v{d}:/mnt", "x,{d}", "scp:{d}", "PATH=/usr/bin:{d}/bin"],
)
def test_a_path_glued_inside_a_word_is_refused(tmp_path: Path, word: str) -> None:
    denied = tmp_path / "clients"
    guardrails = _directive(
        tmp_path, _guardrails(removed=[_removed("folders", str(denied), "agent")])
    )

    assert _folders(tmp_path, denied).refusal([word.format(d=denied)], tmp_path) == 0
    assert guardrails.script_folder_refusal(f"curl -d {word.format(d=denied)} x", tmp_path)


def test_no_removed_folder_refuses_no_word(tmp_path: Path) -> None:
    assert DeniedFolders(()).refusal(["$ANYTHING", "/*", "~"], tmp_path) is None


# ------------------------------------------------------------------ the ACP permission seam


@pytest.fixture
def _no_rlimits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(DirectiveSandbox, "spawn_kwargs", lambda self: {})


def _acp(tmp_path: Path, cli_kind: str) -> AcpRuntime:
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
    )


def _acp_request(
    tmp_path: Path,
    scenario: dict[str, Any],
    cli_kind: str,
    guardrails: Guardrails,
    *,
    fallback: PermissionFallback = PermissionFallback.HOLD,
) -> DirectiveRequest:
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
        permission_fallback=fallback,
        guardrails=guardrails,
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
        {"title": "Edit", "rawInput": {"changes": {"{denied}/a.txt": {"add": "x"}}}},
    ],
)
async def test_an_acp_permission_request_for_a_removed_folder_is_refused_naming_the_layer(
    tmp_path: Path, tool_call: dict[str, Any]
) -> None:
    denied = tmp_path / "clients"
    request = json.loads(json.dumps(tool_call).replace("{denied}", str(denied)))
    guardrails = _guardrails(removed=[_removed("folders", str(denied), "runner")])

    result = await _acp(tmp_path, "codex_cli").execute_directive(
        _acp_request(
            tmp_path,
            {"permissions": [request], "stop_reason": "cancelled"},
            "codex_cli",
            guardrails,
        )
    )

    answers = [m["result"]["outcome"] for m in _received(tmp_path) if "method" not in m]
    assert answers == [{"outcome": "selected", "optionId": "cancel"}]
    assert result.guardrail_refusals == (
        GuardrailRefusal(GuardrailPart.FOLDERS, str(denied), GuardrailLayer.RUNNER),
    )
    # Held for nobody: a removed folder is no question for a person.
    assert "held" not in " ".join(result.evidence.notes)


@pytest.mark.asyncio
@pytest.mark.usefixtures("_no_rlimits")
async def test_an_acp_command_the_compiled_commands_omit_is_refused_naming_the_layer(
    tmp_path: Path,
) -> None:
    guardrails = _guardrails(commands=["pytest"])
    request = {"title": "make", "rawInput": {"command": "make deploy"}}

    result = await _acp(tmp_path, "codex_cli").execute_directive(
        _acp_request(
            tmp_path,
            {"permissions": [request], "stop_reason": "cancelled"},
            "codex_cli",
            guardrails,
            fallback=PermissionFallback.DENY,
        )
    )

    answers = [m["result"]["outcome"] for m in _received(tmp_path) if "method" not in m]
    assert answers == [{"outcome": "selected", "optionId": "cancel"}]
    assert result.guardrail_refusals == (
        GuardrailRefusal(GuardrailPart.COMMANDS, "make", GuardrailLayer.ORGANISATION),
    )
    assert "Refused by the Organisation's Guardrails: commands `make`" in " ".join(
        result.evidence.notes
    )


@pytest.mark.asyncio
@pytest.mark.usefixtures("_no_rlimits")
async def test_claude_code_gets_the_removed_folders_as_its_own_deny_rules(tmp_path: Path) -> None:
    denied = tmp_path / "clients"
    guardrails = _guardrails(removed=[_removed("folders", str(denied), "agent")])

    await _acp(tmp_path, "claude_code").execute_directive(
        _acp_request(tmp_path, {}, "claude_code", guardrails)
    )

    (new,) = _received(tmp_path, "session/new")
    deny = new["params"]["_meta"]["claudeCode"]["options"]["settings"]["permissions"]["deny"]
    resolved = Path(os.path.realpath(denied)).as_posix()
    assert f"Read(/{resolved}/**)" in deny
    assert f"Edit(/{resolved}/**)" in deny


@pytest.mark.asyncio
@pytest.mark.usefixtures("_no_rlimits")
async def test_no_removed_folder_sends_claude_code_no_settings(tmp_path: Path) -> None:
    await _acp(tmp_path, "claude_code").execute_directive(
        _acp_request(tmp_path, {}, "claude_code", Guardrails())
    )

    (new,) = _received(tmp_path, "session/new")
    assert "settings" not in new["params"]["_meta"]["claudeCode"]["options"]


# ------------------------------------------------------------------ limits


def test_the_wall_clock_limit_binds_only_where_it_is_shorter() -> None:
    limited = _guardrails(limits={"wall_clock_seconds": {"value": 60, "layer": "team"}})

    assert turn_timeout(1800, limited) == (
        60,
        GuardrailRefusal(GuardrailPart.LIMITS, "wall_clock_seconds", GuardrailLayer.TEAM),
    )
    assert turn_timeout(30, limited) == (30, None)
    assert turn_timeout(1800, Guardrails()) == (1800, None)
    assert turn_timeout(1800, None) == (1800, None)


@pytest.mark.asyncio
async def test_a_wall_clock_overrun_ends_the_turn_and_names_the_layer(tmp_path: Path) -> None:
    settings = WorkerSettings(
        TEMPORAL_ADDRESS="127.0.0.1:7233",
        INTERNAL_FASTAPI_BASE_URL="http://agentic-api.internal:8000",
        WORKSPACE_ROOT=tmp_path / "workspaces",
        CODEX_HOME=tmp_path / "codex-home",
        CODEX_CLI_TIMEOUT_SECONDS=1800,
        CODEX_SANDBOX_MODE="workspace-write",
        CODEX_ASK_FOR_APPROVAL="never",
        CODEX_STRICT_CONFIG=True,
        CODEX_POLICY_HOOK_CONFIGURED=True,
    )
    workspace = settings.WORKSPACE_ROOT / "c1" / "wr1"
    workspace.mkdir(parents=True)
    timeouts: list[object] = []

    async def overrunning(**kwargs: object) -> object:
        timeouts.append(kwargs["timeout_seconds"])
        raise TimeoutError("the turn ran out of time")

    result = await CodexRuntime(settings=settings, runner=overrunning).execute_directive(  # type: ignore[arg-type]
        DirectiveRequest(
            auth_mode=AuthMode.SUBSCRIPTION,
            workspace_path=workspace,
            prompt="Do the thing",
            base_branch="main",
            work_branch="agent/wr1",
            guardrails=_guardrails(
                limits={"wall_clock_seconds": {"value": 120, "layer": "product"}}
            ),
        )
    )

    assert timeouts == [120]
    assert result.exit_code == 124
    assert result.guardrail_refusals == (
        GuardrailRefusal(GuardrailPart.LIMITS, "wall_clock_seconds", GuardrailLayer.PRODUCT),
    )


@pytest.mark.asyncio
async def test_each_refusal_a_turn_returns_becomes_one_evidence_event() -> None:
    client = _FakeClient()
    activities = RunnerRalphActivities(client)  # type: ignore[arg-type]
    result = DirectiveResult(
        exit_code=124,
        stdout="",
        stderr="",
        error="timed out",
        command_hash="h",
        evidence=DirectiveEvidence(
            workspace_id="w",
            base_branch="main",
            work_branch="agent/wr1",
            command_hash="h",
            guard_mode="g",
        ),
        guardrail_refusals=(
            GuardrailRefusal(GuardrailPart.COMMANDS, "make", GuardrailLayer.ORGANISATION),
            GuardrailRefusal(GuardrailPart.LIMITS, "wall_clock_seconds", GuardrailLayer.AGENT),
        ),
    )

    await activities._record_guardrail_refusals(
        WORK_RECORD_ID, _state(Guardrails()), result, directive_number=3
    )

    assert [(event["layer"], event["directive_number"]) for event in client.refusals()] == [
        ("organisation", 3),
        ("agent", 3),
    ]


def test_the_memory_limit_lowers_the_hosts_ceiling_and_never_raises_it(tmp_path: Path) -> None:
    from agentic_runner.workers.contract_isolation import ContractIsolation

    isolation = ContractIsolation(
        workspace_root=tmp_path / "workspaces",
        state_dir=tmp_path / "state",
        can_separate_uids=False,
        uid_min=20_000,
        uid_max=20_100,
        max_processes=64,
        memory_limit_bytes=4 << 30,
        can_limit_memory=True,
    )

    lower = isolation.sandbox(CONTRACT_ID, runtime_kind="codex_cli", memory_limit_bytes=1 << 30)
    higher = isolation.sandbox(CONTRACT_ID, runtime_kind="codex_cli", memory_limit_bytes=8 << 30)

    assert lower.max_memory_bytes == 1 << 30
    assert higher.max_memory_bytes == 4 << 30


# ------------------------------------------------------------------ posture


def test_posture_is_the_egress_posture_and_commands_and_folders_per_harness(
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

    posture = guardrails_posture(
        build_agent_runtimes(settings, clis),
        egress_posture=EgressPosture.UNENFORCED,
        can_limit_memory=False,
    )

    assert posture.network is EgressPosture.UNENFORCED
    expected = {"claude_code": GuardrailPosture.CHECKED, "codex_cli": GuardrailPosture.UNENFORCED}
    assert posture.commands == expected
    assert posture.folders == expected
    assert posture.memory is GuardrailPosture.UNENFORCED
    assert posture.wall_clock is GuardrailPosture.CHECKED
