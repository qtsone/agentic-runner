"""The contracts 3.25 wire for LLM access (QTS-1736, Spec: Runners rev 3).

What each side must agree on, and how a Runner on 3.25 still talks to a control plane on
3.24: the key-name table, the ack's Contract terms, the envelope's report and install,
and the two retired fields an older control plane still sends.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from agentic_runner_contracts import __version__ as contracts_version
from agentic_runner_contracts.llm_access import (
    PROVIDER_KEY_NAMES,
    LlmAccessKind,
    LlmProvider,
    key_name_for,
    provider_for,
)
from agentic_runner_contracts.runner_registration import (
    CeilingPush,
    ContractLlmAccess,
    ContractLlmTerms,
    FloorState,
    HeartbeatAck,
    ProviderKeyReport,
    RuntimeSubscriptionReport,
    SlotProbe,
    SubscriptionRight,
    SubscriptionState,
)

CONTRACT = UUID("11111111-1111-4111-8111-111111111111")


def test_each_agent_runtime_has_one_provider_and_key_name() -> None:
    assert (provider_for("claude_code"), key_name_for("claude_code")) == (
        LlmProvider.ANTHROPIC,
        "ANTHROPIC_API_KEY",
    )
    assert (provider_for("codex_cli"), key_name_for("codex_cli")) == (
        LlmProvider.OPENAI,
        "OPENAI_API_KEY",
    )
    assert (provider_for("gemini_cli"), key_name_for("gemini_cli")) == (None, None)
    assert set(PROVIDER_KEY_NAMES) == set(LlmProvider)
    assert {kind.value for kind in LlmAccessKind} == {"subscription", "host_key", "llm_key"}


def _ack(**fields: Any) -> dict[str, Any]:
    return {
        "runner_id": str(uuid4()),
        "floor_state": FloorState.OK.value,
        "contracts_floor": contracts_version,
        "tag_set_version": 1,
        "accepts_new_directives": True,
        **fields,
    }


def test_an_ack_names_the_contracts_only_when_it_has_them_to_name() -> None:
    """A Runner on 3.24 forbids the key; None is a control plane below 3.25, and an empty
    list -- still sent -- says the Runner serves no Contract yet."""

    assert "llm_contracts" not in HeartbeatAck.model_validate(_ack()).model_dump(mode="json")
    empty = HeartbeatAck.model_validate(_ack(llm_contracts=[]))
    assert empty.model_dump(mode="json")["llm_contracts"] == []
    named = HeartbeatAck.model_validate(
        _ack(llm_contracts=[{"contract_id": str(CONTRACT), "subscription": "not_acknowledged"}])
    )
    assert named.llm_contracts == [
        ContractLlmTerms(contract_id=CONTRACT, subscription=SubscriptionRight.NOT_ACKNOWLEDGED)
    ]


@pytest.mark.parametrize(("org_funded", "organisation_limit"), [(True, 1_000), (False, None)])
def test_a_323_ceiling_push_is_read_the_way_it_was_meant(
    org_funded: bool, organisation_limit: int | None
) -> None:
    """3.24 pushed the Organisation's ceiling for every Contract and said with `org_funded`
    whether it applied; 3.25 pushes it only where it applies."""

    push = CeilingPush.model_validate(
        {
            "contract_id": str(CONTRACT),
            "contract_limit": 50,
            "organisation_limit": 1_000,
            "organisation_used": 10,
            "org_funded": org_funded,
        }
    )

    assert push.organisation_limit == organisation_limit
    assert push.contract_limit == 50
    assert "org_funded" not in push.model_dump()


def test_a_contracts_llm_access_report_round_trips() -> None:
    report = ContractLlmAccess(
        contract_id=CONTRACT,
        keys=[ProviderKeyReport(provider=LlmProvider.OPENAI, held=True, probe=SlotProbe.VALID)],
        subscriptions=[
            RuntimeSubscriptionReport(
                cli_kind="claude_code",
                state=SubscriptionState.AT_LIMIT,
                resets_at=datetime(2026, 10, 17, 3, tzinfo=UTC),
            )
        ],
    )

    assert ContractLlmAccess.model_validate_json(report.model_dump_json()) == report
    with pytest.raises(ValidationError):
        ProviderKeyReport.model_validate({"provider": "openai", "held": True, "value": "sk-x"})


def test_an_ack_names_its_contracts_version_only_to_a_runner_that_parses_it() -> None:
    """QTS-1773: a Runner on 3.26 forbids the key, so a control plane below 3.27 -- None --
    omits it, and the Runner reads its absence as "do not send `install.name`"."""

    assert "contracts_version" not in HeartbeatAck.model_validate(_ack()).model_dump(mode="json")
    ack = HeartbeatAck.model_validate(_ack(contracts_version="3.27.0"))
    assert ack.model_dump(mode="json")["contracts_version"] == "3.27.0"
