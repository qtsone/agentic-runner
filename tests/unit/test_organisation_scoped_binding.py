"""An Organisation-scoped Work Record's Workspace and binding (ADR-0018 §5-§7).

Before its first write the Work Record has no repository: its Workspace is an empty
directory, ``agentic-runner repo read`` checks a repository in reach out beneath it, and
``repo branch`` binds the Product that owns the repository. Every one of those calls is
the same Grant evaluation the activity seams run, plus two rules that only narrow it: a
write on a second Product's repository is refused (§5), and the Runner refuses a
repository whose Product's selector its own tags do not satisfy (§6).
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from collections.abc import Iterator, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from agentic_runner.activities import (
    RunnerRalphActivities,
    _RuntimeContextState,
    _with_binding,
)
from agentic_runner.callback import AttemptCallbackServer, call
from agentic_runner.integrations.git.fake_workspace import FakeGitWorkspace
from agentic_runner_contracts.activity_io import FixDirectiveOutput, ProductBinding
from agentic_runner_contracts.grants import GrantSnapshot
from agentic_runner_contracts.routing import RunnerRoutingIdentity

WORK_RECORD_ID = "123e4567-e89b-12d3-a456-426614174000"
AGENT_ID = "8f14e45f-ceea-467a-9a37-1a2b3c4d5e6f"
CONTRACT_ID = "1d0f6b8c-2e3f-4a5b-8c7d-9e0f1a2b3c4d"
RUNNER_ID = UUID("7c6d5e4f-3a2b-4c1d-8e9f-0a1b2c3d4e5f")
PRODUCT_APP = "0b1c2d3e-4f50-4617-8293-a4b5c6d7e8f9"
PRODUCT_LIB = "9f8e7d6c-5b4a-4392-8170-6f5e4d3c2b1a"
APP = "acme/app"
LIB = "acme/lib"


class _Client:
    def __init__(self, *, bind: Mapping[str, Any] | None = None) -> None:
        self.evidence: list[tuple[str, dict[str, Any]]] = []
        self.bindings: list[tuple[str, str]] = []
        self._bind = bind

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

    async def bind_product(
        self, work_record_id: str, *, repository: str, base_branch: str
    ) -> dict[str, Any]:
        self.bindings.append((repository, base_branch))
        if self._bind is not None:
            return dict(self._bind)
        product_id = PRODUCT_APP if repository == APP else PRODUCT_LIB
        return {"decision": "allow", "product_id": product_id}

    def evaluations(self) -> list[dict[str, Any]]:
        return [payload for source, payload in self.evidence if source == "ralph.grant_evaluation"]


def _grant() -> dict[str, Any]:
    verbs = {"read": "allow", "branch": "allow", "push": "allow", "pr.open": "allow"}
    return {"entries": [{"resource_type": "repo", "selector": "acme/*", "verbs": verbs}]}


def _snapshot(*, app_selector: Mapping[str, str] | None = None) -> GrantSnapshot:
    return GrantSnapshot.from_payload(
        {
            "agent_id": AGENT_ID,
            "contract_id": CONTRACT_ID,
            "contract_state": "active",
            "dispatchable": True,
            "root_grant": _grant(),
            "agent_grant": _grant(),
            "persona_allow_list": _grant(),
            "resources": [
                {
                    "resource_type": "repo",
                    "selector": APP,
                    "in_contract_scope": True,
                    "product_id": PRODUCT_APP,
                },
                {
                    "resource_type": "repo",
                    "selector": LIB,
                    "in_contract_scope": True,
                    "product_id": PRODUCT_LIB,
                },
            ],
            "reach": [
                {"product_id": PRODUCT_APP, "runner_selector": dict(app_selector or {})},
                {"product_id": PRODUCT_LIB, "runner_selector": {}},
            ],
        }
    )


def _state(
    *, repository: str = "", product_id: str | None = None, snapshot: GrantSnapshot | None = None
) -> _RuntimeContextState:
    return _RuntimeContextState(
        reviewer=None,
        repository=repository,
        verifier_argv=("true",),
        work_branch=f"agent/{WORK_RECORD_ID}-work",
        completion_criteria="look around",
        agent_id=AGENT_ID,
        contract_id=CONTRACT_ID,
        grant_snapshot=snapshot or _snapshot(),
        product_id=product_id,
    )


@pytest.fixture
def socket_dir() -> Iterator[Path]:
    # `sun_path` is ~104 bytes; pytest's `tmp_path` is too long to bind a socket under.
    path = Path(tempfile.mkdtemp(dir="/tmp"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _activities(
    client: _Client, root: Path, git: FakeGitWorkspace, *, tags: Mapping[str, str] | None = None
) -> RunnerRalphActivities:
    return RunnerRalphActivities(
        client,  # type: ignore[arg-type]
        git_workspace=git,
        workspace_root=root,
        routing_identity=RunnerRoutingIdentity(
            runner_id=RUNNER_ID, host_party="organisation", tags=dict(tags or {})
        ),
    )


async def _post(server: AttemptCallbackServer, path: str, payload: dict[str, Any]) -> Any:
    return await asyncio.to_thread(
        call, socket_path=server.socket_path, token=server.token, path=path, payload=payload
    )


def _workspace(root: Path) -> Path:
    workspace = root / CONTRACT_ID / WORK_RECORD_ID
    workspace.mkdir(parents=True)
    return workspace


@pytest.mark.asyncio
async def test_reads_two_products_then_branch_binds_one_and_refuses_the_other(
    tmp_path: Path, socket_dir: Path
) -> None:
    client = _Client()
    git = FakeGitWorkspace()
    activities = _activities(client, tmp_path, git)
    workspace = _workspace(tmp_path)
    bound: list[ProductBinding] = []
    server = AttemptCallbackServer(
        socket_path=socket_dir / "s.sock",
        handlers=activities._callback_handlers(WORK_RECORD_ID, _state(), workspace, bound=bound),
    )

    async with server:
        read_app = await _post(server, "/v0/repo/read", {"repository": APP})
        read_lib = await _post(server, "/v0/repo/read", {"repository": LIB})
        branched = await _post(server, "/v0/repo/branch", {"repository": APP})
        push_lib = await _post(server, "/v0/verb", {"verb": "push", "resource": LIB})
        branch_lib = await _post(server, "/v0/repo/branch", {"repository": LIB})

    assert read_app["allowed"] and read_lib["allowed"]
    assert read_app["path"] == str((workspace / "acme" / "app").resolve())
    assert read_lib["path"] == str((workspace / "acme" / "lib").resolve())
    assert branched["allowed"] is True
    assert branched["product_id"] == PRODUCT_APP
    assert client.bindings == [(APP, "main")]
    assert bound == [ProductBinding(repository=APP, base_ref="main", product_id=PRODUCT_APP)]
    work_branches = [call.operation for call in git.calls if call.repo_full_name == APP]
    assert work_branches[-1] == "checkout_work_branch"

    assert push_lib["allowed"] is False
    assert PRODUCT_APP in push_lib["reason"] and "work.open" in push_lib["reason"]
    assert branch_lib["allowed"] is False
    refusals = [
        evaluation
        for evaluation in client.evaluations()
        if evaluation.get("event") == "work_record.second_product_refused"
    ]
    assert [(refusal["verb"], refusal["resource"]) for refusal in refusals] == [
        ("push", LIB),
        ("branch", LIB),
    ]
    assert all(refusal["product_id"] == PRODUCT_LIB for refusal in refusals)
    assert all(refusal["bound_product_id"] == PRODUCT_APP for refusal in refusals)
    assert all(refusal["remedy"] == "work.open" for refusal in refusals)


@pytest.mark.asyncio
async def test_a_later_directive_refuses_a_push_on_the_other_product(tmp_path: Path) -> None:
    """After binding, the runtime context names the Product, and the push seam holds it."""

    client = _Client()
    activities = _activities(client, tmp_path, FakeGitWorkspace())
    state = _state(repository=APP, product_id=PRODUCT_APP)

    other = await activities._authorize(WORK_RECORD_ID, state, verb="push", repository=LIB)
    own = await activities._authorize(WORK_RECORD_ID, state, verb="push", repository=APP)

    assert other.refused and not own.refused
    refused, allowed = client.evaluations()
    assert refused["decision"] == "deny"
    assert refused["event"] == "work_record.second_product_refused"
    assert refused["product_id"] == PRODUCT_LIB
    assert allowed["decision"] == "allow" and "event" not in allowed


@pytest.mark.asyncio
async def test_a_runner_whose_tags_miss_the_products_selector_refuses_the_repository(
    tmp_path: Path, socket_dir: Path
) -> None:
    client = _Client()
    git = FakeGitWorkspace()
    activities = _activities(client, tmp_path, git, tags={"zone": "us"})
    workspace = _workspace(tmp_path)
    state = _state(snapshot=_snapshot(app_selector={"zone": "eu"}))
    server = AttemptCallbackServer(
        socket_path=socket_dir / "s.sock",
        handlers=activities._callback_handlers(WORK_RECORD_ID, state, workspace),
    )

    async with server:
        read_app = await _post(server, "/v0/repo/read", {"repository": APP})
        branch_app = await _post(server, "/v0/repo/branch", {"repository": APP})
        read_lib = await _post(server, "/v0/repo/read", {"repository": LIB})

    assert read_app["allowed"] is False and branch_app["allowed"] is False
    assert "selector" in read_app["reason"]
    assert read_lib["allowed"] is True
    assert client.bindings == []
    assert not (workspace / "acme" / "app").exists()
    refused = [
        evaluation
        for evaluation in client.evaluations()
        if evaluation.get("event") == "directive.repository_refused"
    ]
    assert [(entry["verb"], entry["resource"]) for entry in refused] == [
        ("read", APP),
        ("branch", APP),
    ]
    assert refused[0]["selector"] == {"zone": "eu"}
    assert refused[0]["runner_id"] == str(RUNNER_ID)


@pytest.mark.asyncio
async def test_a_binding_the_control_plane_refuses_binds_nothing(
    tmp_path: Path, socket_dir: Path
) -> None:
    client = _Client(bind={"decision": "deny", "reason": "acme/app is out of reach"})
    activities = _activities(client, tmp_path, FakeGitWorkspace())
    workspace = _workspace(tmp_path)
    bound: list[ProductBinding] = []
    server = AttemptCallbackServer(
        socket_path=socket_dir / "s.sock",
        handlers=activities._callback_handlers(WORK_RECORD_ID, _state(), workspace, bound=bound),
    )

    async with server:
        branched = await _post(server, "/v0/repo/branch", {"repository": APP, "base_ref": "dev"})

    assert branched == {
        "repository": APP,
        "allowed": False,
        "decision": "deny",
        "reason": "acme/app is out of reach",
        "path": "",
        "product_id": "",
    }
    assert client.bindings == [(APP, "dev")]
    assert bound == []


@pytest.mark.asyncio
async def test_a_bound_work_record_reads_and_branches_nothing_new(
    tmp_path: Path, socket_dir: Path
) -> None:
    client = _Client()
    activities = _activities(client, tmp_path, FakeGitWorkspace())
    workspace = _workspace(tmp_path)
    state = _state(repository=APP, product_id=PRODUCT_APP)
    server = AttemptCallbackServer(
        socket_path=socket_dir / "s.sock",
        handlers=activities._callback_handlers(WORK_RECORD_ID, state, workspace),
    )

    async with server:
        read = await _post(server, "/v0/repo/read", {"repository": LIB})

    assert read["allowed"] is False
    assert APP in read["reason"]
    assert client.bindings == []


def test_the_binding_makes_the_checkout_the_workspace(tmp_path: Path) -> None:
    output = FixDirectiveOutput(
        work_record_id=WORK_RECORD_ID,
        repository="",
        pr_number=0,
        directive_number=1,
        branch_head_sha="",
        summary="directive 1 changed nothing",
        workspace_path=str(tmp_path),
    )
    binding = ProductBinding(repository=APP, base_ref="main", product_id=PRODUCT_APP)

    assert _with_binding(output, []) == output
    assert _with_binding(output, [binding]) == replace(
        output,
        product_binding=binding,
        workspace_path=str(tmp_path.resolve() / "acme" / "app"),
    )
