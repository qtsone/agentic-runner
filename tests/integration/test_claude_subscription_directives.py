"""A Claude subscription on the default Claude Code runtime (local-agents 07, QTS-1737).

Driven through the real ``create_or_update_branch_pr`` activity, the runtime
``build_agent_runtimes`` picks with no ``ACP_CLI_KINDS``, the real subprocess runner and the
real sign-in launcher, against a stand-in ``claude`` on ``PATH`` that records what it was
started with. So what is asserted is what the vendor CLI would see: the sign-in in the
Contract's harness root and no key, and what the Runner made of the CLI's own output.
"""

from __future__ import annotations

import contextlib
import json
import resource
import sys
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agentic_runner.activities import (
    HARNESS_HOLD_SOURCE,
    RunnerRalphActivities,
    _model_usage_reports,
)
from agentic_runner.harness_self_test import DirectivesInFlight
from agentic_runner.integrations.git.fake_workspace import FakeGitWorkspace
from agentic_runner.integrations.github.fake_client import FakeGitHubClient
from agentic_runner.llm_proxy import attempt_env
from agentic_runner.service import build_agent_runtimes, build_usage_windows
from agentic_runner.usage_windows import UsageWindows
from agentic_runner.workers.agent_runtime import AgentRuntime, ModelUsage
from agentic_runner.workers.claude_runtime import ClaudeRuntime
from agentic_runner.workers.claude_sign_in import OAUTH_TOKEN_FILE
from agentic_runner.workers.contract_isolation import ContractIsolation, DirectiveSandbox
from agentic_runner.workers.settings import WorkerSettings
from agentic_runner_contracts.activity_io import (
    BranchPullRequestInput,
    HarnessHold,
    HarnessHoldKind,
)
from agentic_runner_contracts.routing import DirectiveRouting, RunnerRoutingIdentity
from agentic_runner_contracts.runner_registration import CliVersion, UsageWindow
from agentic_runner_contracts.runtime_context import work_branch_name

WORK_RECORD_ID = "123e4567-e89b-12d3-a456-426614174000"
CONTRACT_ID = "11111111-1111-4111-8111-111111111111"
RUNNER_ID = "runner-1"
MODEL = "claude-opus-4-8"
SIGN_IN_TOKEN = "sk-ant-oat01-" + "s" * 40
HOST_API_KEY = "sk-ant-api03-" + "h" * 40

# The stand-in `claude`: records its argv and environment next to itself, then prints the
# result the `mode` file next to it names, as `claude --print --output-format json` does.
_FAKE_CLAUDE = """\
import json, os, sys
from pathlib import Path

here = Path(__file__).parent
sys.stdin.read()
(here / "seen.json").write_text(json.dumps({"argv": sys.argv[1:], "env": dict(os.environ)}))
mode = (here / "mode").read_text().strip()
results = {
    "ok": (0, {
        "type": "result", "subtype": "success", "is_error": False, "result": "done",
        "modelUsage": {"claude-opus-4-8": {
            "inputTokens": 12, "outputTokens": 3,
            "cacheReadInputTokens": 5, "cacheCreationInputTokens": 2,
        }},
    }),
    "limit": (1, {
        "type": "result", "subtype": "success", "is_error": True,
        "result": "You've hit your limit \\u00b7 resets 4pm (UTC)", "modelUsage": {},
    }),
    # Claude Code 2.1.299's own reply with no sign-in in its config root.
    "signed_out": (1, {
        "type": "result", "subtype": "success", "is_error": True,
        "result": "Not logged in \\u00b7 Please run /login", "modelUsage": {},
    }),
}
code, result = results[mode]
print(json.dumps(result))
sys.exit(code)
"""


class _Client:
    def __init__(self) -> None:
        self.evidence: list[tuple[str, dict[str, Any]]] = []
        self.usage: list[dict[str, Any]] = []

    async def get_runtime_context(self, work_record_id: str) -> dict[str, Any]:
        return {
            "work_record_id": WORK_RECORD_ID,
            "contract_id": CONTRACT_ID,
            "profile_slug": "engineer",
            "cli_kind": "claude_code",
            "repo": "qts/agentic-os",
            "base_branch": "main",
            "completion_criteria": "Add a Production section",
            "task_queue": "ralph-pr-loop",
            "worker_secret_refs": [],
            "product_verifier_command_source": {
                "source": "runtime-profile",
                "available": True,
                "metadata": {"command": "python -m pytest -q"},
            },
        }

    async def get_directive_usage(self, work_record_id: str, directive_id: str) -> dict[str, Any]:
        return {}

    async def report_harness_usage(self, payload: Mapping[str, Any]) -> list[dict[str, Any]]:
        self.usage.append(dict(payload))
        return []

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

    async def transition_work_record(
        self, work_record_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return {"ok": True}

    def payloads(self, source: str) -> list[dict[str, Any]]:
        return [payload for seen, payload in self.evidence if seen == source]


class _ClaudeProxy:
    """Holds a key for the Contract; hands each attempt Claude Code's proxy pair."""

    def __init__(self) -> None:
        self.slots = SimpleNamespace(holds=lambda _contract_id: True)

    @contextlib.asynccontextmanager
    async def attempt(self, **_admitted: Any) -> AsyncIterator[Any]:
        yield SimpleNamespace(
            env=lambda cli_kind: attempt_env(
                cli_kind, base_url="http://127.0.0.1:1/a/x", token="attempt-bearer"
            )
        )


@pytest.fixture(autouse=True)
def _no_real_rlimits(monkeypatch: pytest.MonkeyPatch) -> None:
    # As in test_claude_sign_in.py: macOS misreports RLIMIT_DATA's hard cap.
    monkeypatch.setattr(resource, "setrlimit", lambda *_args, **_kwargs: None)


@pytest.fixture
def fake_claude(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The stand-in's directory, first on the Runner's PATH, with an API key in the
    Runner's own environment that must never reach it."""

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "claude"
    script.write_text(f"#!{sys.executable}\n{_FAKE_CLAUDE}")
    script.chmod(0o755)
    (bin_dir / "mode").write_text("ok")
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    monkeypatch.setenv("ANTHROPIC_API_KEY", HOST_API_KEY)
    return bin_dir


def _settings(tmp_path: Path) -> WorkerSettings:
    return WorkerSettings(
        TEMPORAL_ADDRESS="127.0.0.1:7233",
        INTERNAL_FASTAPI_BASE_URL="http://agentic-api.internal:8000",
        WORKSPACE_ROOT=tmp_path / "workspaces",
        CODEX_HOME=tmp_path / "codex-home",
        CLAUDE_MODEL=MODEL,
        CLAUDE_DANGEROUSLY_SKIP_PERMISSIONS=True,
    )


def _default_runtimes(tmp_path: Path) -> dict[str, AgentRuntime]:
    settings = _settings(tmp_path)
    assert settings.ACP_CLI_KINDS == ""
    return build_agent_runtimes(
        settings, [CliVersion(cli_kind="claude_code", version="2.1.299", meets_floor=True)]
    )


def _isolation(tmp_path: Path) -> ContractIsolation:
    return ContractIsolation(
        workspace_root=tmp_path / "workspaces",
        state_dir=tmp_path / ".state",
        uid_min=20_000,
        uid_max=20_100,
        max_processes=64,
        memory_limit_bytes=1 << 30,
        can_separate_uids=False,
    )


def _sign_in(isolation: ContractIsolation) -> None:
    """What `agentic-runner sign-in` leaves for the default method (QTS-1448)."""

    root = isolation.sandbox(CONTRACT_ID, runtime_kind="claude_code").harness_config_dir
    (root / OAUTH_TOKEN_FILE).write_text(SIGN_IN_TOKEN)


def _activities(
    client: _Client,
    tmp_path: Path,
    *,
    isolation: ContractIsolation,
    proxy: _ClaudeProxy | None = None,
    usage_windows: UsageWindows | None = None,
) -> RunnerRalphActivities:
    return RunnerRalphActivities(
        client,  # type: ignore[arg-type]
        agent_runtimes=_default_runtimes(tmp_path),
        git_workspace=FakeGitWorkspace(status_evidence=" M README.md", diff_evidence="+change"),
        github_client=FakeGitHubClient(),
        workspace_root=tmp_path / "workspaces",
        routing_identity=RunnerRoutingIdentity(runner_id=RUNNER_ID, host_party="user", tags={}),
        llm_proxy=proxy,  # type: ignore[arg-type]
        contract_isolation=isolation,
        usage_windows=usage_windows,
    )


def _branch_pr_input() -> BranchPullRequestInput:
    return BranchPullRequestInput(
        work_record_id=WORK_RECORD_ID,
        repository="qts/agentic-os",
        base_ref="main",
        branch_name=work_branch_name(work_record_id=WORK_RECORD_ID, profile_slug="engineer"),
        pr_title="Add a Production section",
        pr_body="body",
        routing=DirectiveRouting(runner_id=RUNNER_ID, host_party="user"),
    )


def _seen(fake_claude: Path) -> dict[str, Any]:
    seen: dict[str, Any] = json.loads((fake_claude / "seen.json").read_text())
    return seen


def test_a_default_install_runs_claude_code_on_the_per_cli_runtime_with_both_modes(
    tmp_path: Path,
) -> None:
    runtime = _default_runtimes(tmp_path)["claude_code"]

    assert isinstance(runtime, ClaudeRuntime)
    assert runtime.capabilities().auth_modes == {"api_key", "subscription"}


@pytest.mark.asyncio
async def test_a_subscription_directive_runs_on_the_harness_roots_sign_in_and_finishes(
    tmp_path: Path, fake_claude: Path
) -> None:
    client = _Client()
    isolation = _isolation(tmp_path)
    _sign_in(isolation)

    output = await _activities(client, tmp_path, isolation=isolation).create_or_update_branch_pr(
        _branch_pr_input()
    )

    assert output.harness_hold is None
    assert output.branch_head_sha
    seen = _seen(fake_claude)
    harness_root = isolation.harness_config_dir(CONTRACT_ID, "claude_code")
    assert seen["env"]["CLAUDE_CONFIG_DIR"] == str(harness_root)
    # The launcher, as the Contract's own process, handed the CLI the sign-in it reads.
    assert seen["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == SIGN_IN_TOKEN
    assert seen["env"]["CLAUDE_CODE_SUBPROCESS_ENV_SCRUB"] == "1"
    assert "--bare" not in seen["argv"]
    assert seen["argv"][seen["argv"].index("--setting-sources") + 1] == ""
    assert seen["argv"][seen["argv"].index("--model") + 1] == MODEL


@pytest.mark.asyncio
async def test_a_subscription_directive_holds_no_key_and_the_runner_relays_no_token(
    tmp_path: Path, fake_claude: Path
) -> None:
    client = _Client()
    isolation = _isolation(tmp_path)
    _sign_in(isolation)

    output = await _activities(client, tmp_path, isolation=isolation).create_or_update_branch_pr(
        _branch_pr_input()
    )

    env = _seen(fake_claude)["env"]
    assert not {"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"} & set(env)
    assert HOST_API_KEY not in json.dumps(env)
    # Nothing the Runner sent the control plane carries the sign-in.
    sent = json.dumps([client.evidence, client.usage], default=str) + repr(output)
    assert SIGN_IN_TOKEN not in sent


@pytest.mark.asyncio
async def test_a_subscription_directives_usage_is_reported_as_the_acp_path_reports_it(
    tmp_path: Path, fake_claude: Path
) -> None:
    client = _Client()
    isolation = _isolation(tmp_path)
    _sign_in(isolation)

    await _activities(client, tmp_path, isolation=isolation).create_or_update_branch_pr(
        _branch_pr_input()
    )

    [usage] = client.usage
    ((acp_event, acp_model),) = _model_usage_reports(
        "claude_code",
        [ModelUsage(MODEL, 12, 3, cached_read_tokens=5, cached_write_tokens=2)],
    )
    assert (usage["runtime_kind"], usage["contract_id"]) == ("claude_code", CONTRACT_ID)
    assert usage["model_name"] == acp_model
    assert usage["usage_event"]["type"] == acp_event["type"]
    assert usage["usage_event"]["modelUsage"] == acp_event["modelUsage"]


@pytest.mark.asyncio
async def test_the_same_directive_with_an_api_key_runs_on_the_proxy_as_today(
    tmp_path: Path, fake_claude: Path
) -> None:
    client = _Client()
    isolation = _isolation(tmp_path)
    _sign_in(isolation)

    await _activities(
        client, tmp_path, isolation=isolation, proxy=_ClaudeProxy()
    ).create_or_update_branch_pr(_branch_pr_input())

    seen = _seen(fake_claude)
    assert "--bare" in seen["argv"]
    assert "--setting-sources" not in seen["argv"]
    assert seen["env"]["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:1/a/x"
    assert seen["env"]["ANTHROPIC_AUTH_TOKEN"] == "attempt-bearer"
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in seen["env"]
    assert "ANTHROPIC_API_KEY" not in seen["env"]
    # The proxy metered it; the harness's own report would count it twice.
    assert client.usage == []


@pytest.mark.asyncio
async def test_a_usage_limit_holds_the_subscription_until_the_reset(
    tmp_path: Path, fake_claude: Path
) -> None:
    (fake_claude / "mode").write_text("limit")
    client = _Client()
    isolation = _isolation(tmp_path)
    _sign_in(isolation)

    output = await _activities(client, tmp_path, isolation=isolation).create_or_update_branch_pr(
        _branch_pr_input()
    )

    assert output.harness_hold is not None
    assert output.harness_hold.kind is HarnessHoldKind.USAGE_LIMIT
    assert output.harness_hold.retry_not_before is not None
    assert output.harness_hold.retry_not_before.endswith("T16:00:00+00:00")
    assert (output.pr_created, output.branch_head_sha) == (False, "")
    [event] = client.payloads(HARNESS_HOLD_SOURCE)
    assert (event["kind"], event["cli_kind"]) == ("usage_limit", "claude_code")


@pytest.mark.asyncio
async def test_an_expired_sign_in_holds_the_subscription_until_the_person_signs_in(
    tmp_path: Path, fake_claude: Path
) -> None:
    (fake_claude / "mode").write_text("signed_out")
    client = _Client()

    output = await _activities(
        client, tmp_path, isolation=_isolation(tmp_path)
    ).create_or_update_branch_pr(_branch_pr_input())

    assert output.harness_hold == HarnessHold(kind=HarnessHoldKind.SIGN_IN_REQUIRED)
    assert (output.pr_created, output.branch_head_sha) == (False, "")
    [event] = client.payloads(HARNESS_HOLD_SOURCE)
    assert (event["kind"], event["cli_kind"]) == ("sign_in_required", "claude_code")


@pytest.mark.asyncio
async def test_a_usage_limit_makes_the_windows_the_heartbeat_reports_due_at_once(
    tmp_path: Path, fake_claude: Path
) -> None:
    """A default install reads Claude Code's windows with the `/usage` probe, as for the ACP
    runtime; a Directive stopped on its limit makes the next read due at once."""

    (fake_claude / "mode").write_text("limit")
    isolation = _isolation(tmp_path)
    _sign_in(isolation)
    built = build_usage_windows(
        host_party="user",
        runtimes=_default_runtimes(tmp_path),
        isolation=isolation,
        in_flight=DirectivesInFlight(),
    )
    assert built is not None
    assert list(built._probes) == ["claude_code"]
    window = UsageWindow(name="five_hour", used_percent=100.0)
    probes: list[DirectiveSandbox] = []

    async def probe(sandbox: DirectiveSandbox) -> list[UsageWindow]:
        probes.append(sandbox)
        return [window]

    usage = UsageWindows(
        probes={"claude_code": probe},
        sandbox_for=lambda contract_id, cli_kind: isolation.existing_sandbox(
            contract_id, runtime_kind=cli_kind
        ),
        in_flight=DirectivesInFlight(),
        # Time stands still: only the usage limit can make the next probe due.
        monotonic=lambda: 0.0,
    )
    targets = [(CONTRACT_ID, "claude_code")]
    await usage.run_due(targets)

    await _activities(
        _Client(), tmp_path, isolation=isolation, usage_windows=usage
    ).create_or_update_branch_pr(_branch_pr_input())
    await usage.run_due(targets)

    assert len(probes) == 2
    assert usage.windows(CONTRACT_ID, "claude_code") == [window]
