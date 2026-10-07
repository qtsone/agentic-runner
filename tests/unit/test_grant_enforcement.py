"""``decide_verb``: the Runner's local answer for one privileged verb (ADR-0011 §8-12)."""

from __future__ import annotations

from typing import Any

import pytest

from agentic_runner_contracts.grants import (
    UNENFORCED_SNAPSHOT,
    Decision,
    Grant,
    GrantLink,
    GrantSnapshot,
    ResourceRegistration,
)
from agentic_runner_contracts.grants.seam import decide_verb

REPOSITORY = "qtsone/agentic-os"


def _grant(**verbs: str) -> Grant:
    return Grant.model_validate(
        {"entries": [{"resource_type": "repo", "selector": REPOSITORY, "verbs": dict(verbs)}]}
    )


def _snapshot(
    *,
    root: Grant | None = None,
    agent: Grant | None = None,
    persona: Grant | None = None,
    contract_state: str = "active",
    in_contract_scope: bool = True,
) -> GrantSnapshot:
    wide = _grant(push="allow", **{"pr.open": "allow", "pr.review": "allow"})
    return GrantSnapshot(
        agent_id="agent-1",
        contract_id="contract-1",
        contract_state=contract_state,
        dispatchable=contract_state == "active",
        root=root or wide,
        agent=agent or wide,
        persona=persona or wide,
        resources=(
            ResourceRegistration(
                resource_type="repo",
                selector=REPOSITORY,
                in_contract_scope=in_contract_scope,
            ),
        ),
    )


def test_the_chain_allows_only_what_every_link_allows() -> None:
    decision = decide_verb(_snapshot(), verb="push", identifier=REPOSITORY)

    assert decision.allowed
    assert decision.decision is Decision.ALLOW
    assert decision.deciding_link is GrantLink.ROOT


@pytest.mark.parametrize("link", ("root", "agent", "persona"))
def test_a_deny_anywhere_in_the_chain_refuses(link: str) -> None:
    narrowed: dict[str, Any] = {link: _grant(push="deny")}

    decision = decide_verb(_snapshot(**narrowed), verb="push", identifier=REPOSITORY)

    assert not decision.allowed
    assert decision.deciding_link is GrantLink(link)
    assert decision.evidence()["deciding_entry"] == {
        "resource_type": "repo",
        "selector": REPOSITORY,
        "decision": "deny",
    }


def test_a_verb_no_entry_mentions_is_denied_by_default() -> None:
    decision = decide_verb(_snapshot(), verb="pr.merge", identifier=REPOSITORY)

    assert not decision.allowed
    assert "deny by default" in decision.reason


def test_a_confirm_escalates_rather_than_allowing_or_denying() -> None:
    # A `confirm` is a third answer, not a flavour of deny (ADR-0011 s14): the seam must
    # be able to tell "ask the owner" from "never" or it would either refuse work the
    # owner would have consented to, or let an unanswered escalation read as consent.
    decision = decide_verb(
        _snapshot(agent=_grant(push="confirm")), verb="push", identifier=REPOSITORY
    )

    assert not decision.allowed
    assert decision.needs_owner_confirmation is True
    assert decision.decision is Decision.CONFIRM
    assert decision.evidence()["decision"] == "confirm"


def test_an_inactive_contract_refuses_every_verb_it_holds_a_snapshot_for() -> None:
    decision = decide_verb(
        _snapshot(contract_state="suspended"), verb="push", identifier=REPOSITORY
    )

    assert not decision.allowed
    assert "suspended" in decision.reason


def test_a_resource_outside_the_contracts_product_scope_is_unreachable() -> None:
    decision = decide_verb(_snapshot(in_contract_scope=False), verb="push", identifier=REPOSITORY)

    assert not decision.allowed
    assert decision.deciding_link is None


def test_an_unregistered_resource_is_out_of_scope_whatever_the_selector_says() -> None:
    decision = decide_verb(_snapshot(), verb="push", identifier="someone-else/private")

    assert not decision.allowed
    assert decision.deciding_link is None


def test_an_unenforced_snapshot_allows_and_says_so_in_its_evidence() -> None:
    evidence = decide_verb(UNENFORCED_SNAPSHOT, verb="push", identifier=REPOSITORY).evidence()

    assert evidence["decision"] == "allow"
    assert evidence["enforced"] is False
    assert evidence["agent_id"] is None


def test_the_snapshot_parses_the_control_planes_payload_shape() -> None:
    # Pins the wire contract against services/agents.py:GrantSnapshotRead — a field
    # renamed there would otherwise surface as a silent all-deny at the seam.
    payload = {
        "agent_id": "agent-1",
        "contract_id": "contract-1",
        "contract_state": "active",
        "dispatchable": True,
        "root_grant": {"entries": []},
        "root_grant_catalogue_version": "v1",
        "agent_grant": {"entries": []},
        "agent_grant_catalogue_version": "v1",
        "persona_slug": "engineer",
        "persona_allow_list": {"entries": []},
        "contract_product_ids": ["product-1"],
        "resources": [
            {
                "resource_type": "repo",
                "selector": REPOSITORY,
                "product_id": "product-1",
                "in_contract_scope": True,
            }
        ],
    }

    snapshot = GrantSnapshot.from_payload(payload)

    assert snapshot.enforced is True
    assert snapshot.in_contract_scope(resource_type="repo", identifier=REPOSITORY)
    assert not snapshot.in_contract_scope(resource_type="repo", identifier="other/repo")
    # A payload from a backend before contracts 2.2 carries no reach: nothing is reachable
    # by an Organisation-scoped Work Record, and the Product-scoped seams read as before.
    assert snapshot.reach == ()
    assert snapshot.product_of(identifier=REPOSITORY) == "product-1"


def test_the_snapshot_parses_the_reach_of_an_organisation_scoped_work_record() -> None:
    payload = {
        "agent_id": "agent-1",
        "contract_id": "contract-1",
        "contract_state": "active",
        "dispatchable": True,
        "root_grant": {"entries": []},
        "agent_grant": {"entries": []},
        "persona_allow_list": {"entries": []},
        "resources": [
            {
                "resource_type": "repo",
                "selector": REPOSITORY,
                "product_id": "product-1",
                "in_contract_scope": True,
            }
        ],
        "reach": [
            {"product_id": "product-1", "runner_selector": {"pool": "hetzner"}},
            {"product_id": "product-2", "runner_selector": {}},
        ],
    }

    snapshot = GrantSnapshot.from_payload(payload)

    assert snapshot.runner_selector_of("product-1") == {"pool": "hetzner"}
    assert snapshot.runner_selector_of("product-2") == {}
    assert snapshot.runner_selector_of("product-3") is None
    assert snapshot.product_of(identifier="other/repo") is None
