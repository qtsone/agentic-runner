"""The Runner Limit's contract: model, wire field and evaluator link (ADR-0022 §52-§58)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agentic_runner_contracts.grants import (
    PR_MERGE_VERB,
    PR_OPEN_VERB,
    REPO_RESOURCE_TYPE,
    UNENFORCED_SNAPSHOT,
    Decision,
    Grant,
    GrantEntry,
    GrantLink,
    GrantSnapshot,
    Resource,
    ResourceRegistration,
    decide_verb,
    evaluate_effective_grant,
)
from agentic_runner_contracts.runner_limit import RunnerLimit
from agentic_runner_contracts.runner_registration import (
    BootstrapRequest,
    EgressPosture,
    FolderPosture,
    HeartbeatEnvelope,
    IsolationMode,
    RecipientKey,
    ResourcePressure,
    RunnerLimitPosture,
    RunnerLimitReport,
)

REPO = Resource(resource_type=REPO_RESOURCE_TYPE, identifier="qtsone/forge", in_contract_scope=True)
OUT_OF_SCOPE = Resource(
    resource_type=REPO_RESOURCE_TYPE, identifier="acme/secret", in_contract_scope=False
)


def _grant(**verbs: Decision) -> Grant:
    return Grant(
        entries=(GrantEntry(resource_type=REPO_RESOURCE_TYPE, selector="qtsone/*", verbs=verbs),)
    )


ALLOW_BOTH = _grant(**{PR_OPEN_VERB: Decision.ALLOW, PR_MERGE_VERB: Decision.ALLOW})
DENY_MERGE = Grant(
    entries=(
        GrantEntry(
            resource_type=REPO_RESOURCE_TYPE,
            selector="qtsone/forge",
            verbs={PR_MERGE_VERB: Decision.DENY},
        ),
    )
)

# Every shape of answer the chain gives today: allow, confirm, a deny by entry, a deny by
# default, an Account Contract refusal and an out-of-scope resource.
CHAINS = [
    pytest.param(
        {"root": ALLOW_BOTH, "agent": ALLOW_BOTH, "persona": ALLOW_BOTH}, REPO, id="allow"
    ),
    pytest.param(
        {
            "root": ALLOW_BOTH,
            "agent": _grant(**{PR_OPEN_VERB: Decision.ALLOW, PR_MERGE_VERB: Decision.CONFIRM}),
            "persona": ALLOW_BOTH,
        },
        REPO,
        id="confirm",
    ),
    pytest.param({"root": ALLOW_BOTH, "agent": DENY_MERGE, "persona": ALLOW_BOTH}, REPO, id="deny"),
    pytest.param({"root": Grant(), "agent": ALLOW_BOTH, "persona": ALLOW_BOTH}, REPO, id="default"),
    pytest.param(
        {
            "root": ALLOW_BOTH,
            "agent": ALLOW_BOTH,
            "persona": ALLOW_BOTH,
            "account_contract": DENY_MERGE,
        },
        REPO,
        id="account-contract",
    ),
    pytest.param(
        {"root": ALLOW_BOTH, "agent": ALLOW_BOTH, "persona": ALLOW_BOTH}, OUT_OF_SCOPE, id="scope"
    ),
]


@pytest.mark.parametrize(("chain", "resource"), CHAINS)
@pytest.mark.parametrize("verb", [PR_OPEN_VERB, PR_MERGE_VERB])
@pytest.mark.parametrize(
    "runner_limit",
    [
        pytest.param(Grant(), id="empty"),
        pytest.param(RunnerLimit().tools_grant, id="empty-model"),
        pytest.param(
            Grant(
                entries=(
                    GrantEntry(
                        resource_type=REPO_RESOURCE_TYPE,
                        selector="other/*",
                        verbs={"*": Decision.DENY},
                    ),
                )
            ),
            id="non-matching",
        ),
    ],
)
def test_a_limit_that_denies_nothing_here_changes_no_decision(
    chain: dict[str, Grant], resource: Resource, verb: str, runner_limit: Grant
) -> None:
    without = evaluate_effective_grant(**chain, resource=resource, verb=verb)
    with_limit = evaluate_effective_grant(
        **chain, resource=resource, verb=verb, runner_limit=runner_limit
    )

    assert (with_limit.decision, with_limit.deciding_link, with_limit.deciding_entry) == (
        without.decision,
        without.deciding_link,
        without.deciding_entry,
    )
    assert with_limit.reason == without.reason
    assert with_limit.links[: len(without.links)] == without.links


def test_a_matching_deny_wins_over_every_allow_and_is_named() -> None:
    decided = evaluate_effective_grant(
        root=ALLOW_BOTH,
        agent=ALLOW_BOTH,
        persona=ALLOW_BOTH,
        resource=REPO,
        verb=PR_MERGE_VERB,
        runner_limit=DENY_MERGE,
    )

    assert decided.decision is Decision.DENY
    assert decided.deciding_link is GrantLink.RUNNER_LIMIT
    assert decided.deciding_entry == DENY_MERGE.entries[0]
    assert "Runner Limit" in decided.reason


def test_a_deny_overrides_a_confirm_the_owner_would_have_been_asked() -> None:
    decided = evaluate_effective_grant(
        root=ALLOW_BOTH,
        agent=_grant(**{PR_MERGE_VERB: Decision.CONFIRM}),
        persona=ALLOW_BOTH,
        resource=REPO,
        verb=PR_MERGE_VERB,
        runner_limit=DENY_MERGE,
    )

    assert decided.decision is Decision.DENY
    assert decided.deciding_link is GrantLink.RUNNER_LIMIT


def test_a_grant_that_also_refuses_is_named_before_the_limit() -> None:
    # Moving the work to another Runner would not help, so the Evidence names the Grant.
    decided = evaluate_effective_grant(
        root=ALLOW_BOTH,
        agent=DENY_MERGE,
        persona=ALLOW_BOTH,
        resource=REPO,
        verb=PR_MERGE_VERB,
        runner_limit=DENY_MERGE,
    )

    assert decided.deciding_link is GrantLink.AGENT


def test_a_verb_wildcard_in_the_limit_denies_every_verb_it_matches() -> None:
    limit = Grant(
        entries=(
            GrantEntry(
                resource_type=REPO_RESOURCE_TYPE, selector="*", verbs={"pr.*": Decision.DENY}
            ),
        )
    )

    for verb in (PR_OPEN_VERB, PR_MERGE_VERB):
        decided = evaluate_effective_grant(
            root=ALLOW_BOTH,
            agent=ALLOW_BOTH,
            persona=ALLOW_BOTH,
            resource=REPO,
            verb=verb,
            runner_limit=limit,
        )
        assert decided.deciding_link is GrantLink.RUNNER_LIMIT


def test_the_seam_names_the_limit_in_evidence() -> None:
    snapshot = GrantSnapshot(
        agent_id="agent-1",
        root=ALLOW_BOTH,
        agent=ALLOW_BOTH,
        persona=ALLOW_BOTH,
        resources=(
            ResourceRegistration(
                resource_type=REPO_RESOURCE_TYPE, selector="qtsone/*", in_contract_scope=True
            ),
        ),
    )

    decided = decide_verb(
        snapshot, verb=PR_MERGE_VERB, identifier="qtsone/forge", runner_limit=DENY_MERGE
    )

    assert not decided.allowed
    assert decided.evidence()["deciding_link"] == "runner_limit"
    assert decided.evidence()["deciding_entry"] == {
        "resource_type": REPO_RESOURCE_TYPE,
        "selector": "qtsone/forge",
        "decision": "deny",
    }


def test_the_limit_binds_where_no_agent_is_bound() -> None:
    unlimited = decide_verb(UNENFORCED_SNAPSHOT, verb=PR_MERGE_VERB, identifier="qtsone/forge")
    limited = decide_verb(
        UNENFORCED_SNAPSHOT,
        verb=PR_MERGE_VERB,
        identifier="qtsone/forge",
        runner_limit=DENY_MERGE,
    )
    elsewhere = decide_verb(
        UNENFORCED_SNAPSHOT, verb=PR_OPEN_VERB, identifier="qtsone/forge", runner_limit=DENY_MERGE
    )

    assert unlimited.allowed
    assert elsewhere.allowed
    assert not limited.allowed
    assert limited.deciding_link is GrantLink.RUNNER_LIMIT


@pytest.mark.parametrize("decision", ["allow", "confirm"])
def test_a_tools_entry_that_does_not_deny_is_refused(decision: str) -> None:
    with pytest.raises(ValidationError, match="only denies"):
        RunnerLimit.model_validate(
            {
                "tools": [
                    {
                        "resource_type": REPO_RESOURCE_TYPE,
                        "selector": "*",
                        "verbs": {PR_OPEN_VERB: "deny", PR_MERGE_VERB: decision},
                    }
                ]
            }
        )


def test_a_folder_must_be_absolute() -> None:
    with pytest.raises(ValidationError, match="not an absolute path"):
        RunnerLimit(folders=("clients/acme",))


def test_an_empty_limit_is_the_default() -> None:
    assert RunnerLimit() == RunnerLimit(tools=(), networks=(), folders=())


REPORT = RunnerLimitReport(
    limit=RunnerLimit(
        tools=DENY_MERGE.entries,
        networks=("*.internal.example", "db.example:5432"),
        folders=("/Users/someone/clients",),
    ),
    posture=RunnerLimitPosture(
        networks=EgressPosture.UNENFORCED,
        folders={"claude": FolderPosture.CHECKED, "codex": FolderPosture.UNENFORCED},
    ),
)


def _envelope(**fields: object) -> HeartbeatEnvelope:
    return HeartbeatEnvelope.model_validate(
        {
            "runner_version": "3.21.0",
            "contracts_version": "3.21.0",
            "resource_pressure": ResourcePressure(cpu_percent=0, memory_percent=0, disk_percent=0),
            "max_concurrent_directives": 1,
            "current_load": 0,
            "egress_posture": EgressPosture.UNENFORCED,
            "isolation_mode": IsolationMode.NONE,
            "tag_set_version": 1,
            "hosted_task_queue": "runner.00000000-0000-0000-0000-000000000001",
            **fields,
        }
    )


def _bootstrap(**fields: object) -> BootstrapRequest:
    return BootstrapRequest(
        registration_token="registration-token-of-sixteen-plus",
        isolation_mode=IsolationMode.NONE,
        contracts_version="3.21.0",
        runner_version="3.21.0",
        recipient_key=RecipientKey(key_id="key-0001", public_key="k" * 32),
        **fields,
    )


def test_the_heartbeat_round_trips_a_limit_with_its_posture() -> None:
    sent = _envelope(runner_limit=REPORT)

    received = HeartbeatEnvelope.model_validate_json(sent.model_dump_json())

    assert received.runner_limit == REPORT
    assert received.runner_limit is not None
    assert received.runner_limit.posture.tools == "enforced"


def test_registration_round_trips_a_limit_with_its_posture() -> None:
    sent = _bootstrap(runner_limit=REPORT)

    received = BootstrapRequest.model_validate_json(sent.model_dump_json())

    assert received.runner_limit == REPORT


def test_a_body_without_a_limit_parses_and_is_sent_without_the_key() -> None:
    # A Runner below 3.21 sends no key; one on 3.21 with none to report sends the body a
    # control plane below 3.21 already parses.
    envelope = _envelope()
    bootstrap = _bootstrap()

    assert envelope.runner_limit is None
    assert bootstrap.runner_limit is None
    assert "runner_limit" not in envelope.model_dump()
    assert "runner_limit" not in bootstrap.model_dump()


def test_a_posture_the_contract_does_not_know_is_refused() -> None:
    with pytest.raises(ValidationError):
        RunnerLimitPosture.model_validate({"tools": "checked", "networks": "unenforced"})
    with pytest.raises(ValidationError):
        RunnerLimitPosture.model_validate(
            {"networks": "unenforced", "folders": {"claude": "enforced"}}
        )
