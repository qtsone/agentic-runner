"""The Runner's own half of Directive routing (PRD issue 42, map ticket 25 §9, 17 A3).

Two things, both fail-closed and both *before* any activity body runs (the selector
vocabulary itself: ``test_selector_requirements.py``):

* an activity payload this Runner was not routed for is refused with an Evidence Event
  and no verb, no workspace and no runtime turn;
* nothing credential-shaped is in an activity payload at all: the Directive's own
  control-plane token is pulled by id over the heartbeat stream (25 §11), so a Temporal
  history is never a credential store.
"""

from __future__ import annotations

import dataclasses
import re
from typing import Any

import pytest
from temporalio.converter import DataConverter

from agentic_runner.activities import RunnerRalphActivities
from agentic_runner_contracts import activity_io as contracts
from agentic_runner_contracts.llm_access import LlmAccess
from agentic_runner_contracts.routing import (
    DirectiveRouting,
    RoutingRefusedError,
    RunnerRoutingIdentity,
    SelectorOperator,
    SelectorRequirement,
    assert_routed,
)

IDENTITY = RunnerRoutingIdentity(
    runner_id="11111111-1111-4111-8111-111111111111",
    host_party="organisation",
    tags={"region": "eu-west-1", "gpu": "h100"},
)


def _routing(**overrides: Any) -> DirectiveRouting:
    fields: dict[str, Any] = {
        "runner_id": IDENTITY.runner_id,
        "host_party": "organisation",
        "selector": {"region": "eu-west-1"},
        "contract_id": "contract-7",
    }
    fields.update(overrides)
    return DirectiveRouting(**fields)


# --------------------------------------------------------------------------- assertion


def test_the_routed_runner_accepts_its_own_payload() -> None:
    # Returns nothing and raises nothing: the Runner may act.
    assert_routed(_routing(), IDENTITY)


@pytest.mark.parametrize(
    ("routing", "reason"),
    [
        (None, "routing_absent"),
        (_routing(runner_id="22222222-2222-4222-8222-222222222222"), "runner_mismatch"),
        (_routing(host_party="user"), "host_party_mismatch"),
        (_routing(selector={"zone": "trusted"}), "selector_unsatisfied"),
        (
            _routing(
                requirements=(
                    SelectorRequirement(
                        key="gpu", operator=SelectorOperator.NOT_IN, values=("h100",)
                    ),
                )
            ),
            "selector_unsatisfied",
        ),
    ],
)
def test_a_payload_this_runner_was_not_routed_for_is_refused(
    routing: DirectiveRouting | None, reason: str
) -> None:
    with pytest.raises(RoutingRefusedError) as refusal:
        assert_routed(routing, IDENTITY)
    assert refusal.value.reason == reason


def _fix_directive_input(routing: dict[str, Any]) -> contracts.FixDirectiveInput:
    converter = DataConverter.default.payload_converter
    payload = {
        "work_record_id": "wr-1",
        "repository": "acme/app",
        "pr_number": 7,
        "base_ref": "main",
        "branch_name": "agentic/wr-1",
        "directive_number": 2,
        "verifier_summary": "tests failed",
        "routing": routing,
    }
    [encoded] = converter.to_payloads([payload])
    [decoded] = converter.from_payloads([encoded], [contracts.FixDirectiveInput])
    assert isinstance(decoded, contracts.FixDirectiveInput)
    return decoded


def test_the_directive_input_carries_the_llm_access_it_was_routed_with() -> None:
    # Rev 3: the control plane chooses the host's access and the Runner uses only that.
    decoded = _fix_directive_input(
        {
            "runner_id": IDENTITY.runner_id,
            "host_party": "organisation",
            "llm_access": {"kind": "llm_key", "llm_key_id": "key-7"},
        }
    )

    assert decoded.routing is not None
    assert decoded.routing.llm_access == LlmAccess(kind="llm_key", llm_key_id="key-7")


def test_a_directive_input_from_a_323_control_plane_still_decodes() -> None:
    """No `llm_access`, and the retired `funding_party`: the Runner keeps its own choice
    of access, and drops the key its dataclass no longer has."""

    decoded = _fix_directive_input(
        {"runner_id": IDENTITY.runner_id, "host_party": "organisation", "funding_party": "user"}
    )

    assert decoded.routing is not None
    assert decoded.routing.llm_access is None
    assert decoded.routing.requirements == ()


class _RecordingClient:
    """Just the one call the guard makes -- and proof the body never ran."""

    def __init__(self) -> None:
        self.evidence: list[dict[str, Any]] = []

    async def append_evidence(
        self, work_record_id: str, *, source: str, payload: dict[str, Any], **kwargs: Any
    ) -> dict[str, Any]:
        self.evidence.append(
            {"work_record_id": work_record_id, "source": source, "payload": dict(payload)}
        )
        return {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("routing", "reason"),
    [
        (_routing(host_party="user"), "host_party_mismatch"),
        (_routing(selector={"zone": "trusted"}), "selector_unsatisfied"),
    ],
)
async def test_a_misrouted_directive_is_refused_with_evidence_before_the_body_runs(
    routing: DirectiveRouting, reason: str
) -> None:
    client = _RecordingClient()
    activities = RunnerRalphActivities(client, routing_identity=IDENTITY)

    with pytest.raises(RoutingRefusedError):
        await activities.execute_fix_directive(
            contracts.FixDirectiveInput(
                work_record_id="wr-10",
                repository="qts/agentic-os",
                pr_number=42,
                base_ref="main",
                branch_name="ralph/wr-10",
                directive_number=2,
                verifier_summary="pytest failed",
                routing=routing,
            )
        )

    # No workspace, no runtime, no verb: the refusal is the whole activity. The activity
    # has no git workspace or Agent Runtime wired at all, so reaching the body would have
    # raised something else entirely.
    [evidence] = client.evidence
    assert evidence["work_record_id"] == "wr-10"
    assert evidence["payload"]["event"] == "directive.routing_refused"
    assert evidence["payload"]["reason"] == reason


@pytest.mark.asyncio
async def test_a_process_that_never_registered_asserts_nothing() -> None:
    # It polls no `runner.{runner_id}` queue, so nothing routes to it and there is
    # nothing to assert against -- the check is on every Runner the router can pick.
    activities = RunnerRalphActivities(_RecordingClient(), routing_identity=None)
    await activities._assert_routed(
        contracts.FixDirectiveInput(
            work_record_id="wr-10",
            repository="qts/agentic-os",
            pr_number=42,
            base_ref="main",
            branch_name="ralph/wr-10",
            directive_number=2,
            verifier_summary="pytest failed",
        )
    )


# ------------------------------------------------------------------ credential shapes


# What a field name would have to look like for a secret to be riding in a payload. The
# Budget's `max_tokens` and a Directive's reported `tokens` are counts, not credentials.
_CREDENTIAL_SHAPED = re.compile(
    r"(^|_)(token|secret|password|passphrase|credential|api_key|private_key)($|_)"
)
# Counts and booleans that merely *name* a credential. `token_present` is a `stat` of
# the harness token file, never its contents (PRD issue 31's own rule).
_NOT_A_CREDENTIAL = frozenset(
    {"max_tokens", "tokens", "tokens_used", "tokens_consumed", "token_present"}
)


def test_no_activity_payload_field_is_credential_shaped() -> None:
    """Map ticket 25 §11: nothing credential-shaped enters Temporal history.

    A Temporal history is listable by anything with namespace access, so a token in an
    activity payload would be a standing credential in a place nothing revokes. The
    Directive's own control-plane token is pulled by id over the heartbeat stream
    instead (``DirectiveTokenRequest``), which is why this structural check can be
    absolute rather than a list of exceptions.
    """

    offenders: list[str] = []
    for name in dir(contracts):
        candidate = getattr(contracts, name)
        if not dataclasses.is_dataclass(candidate) or not isinstance(candidate, type):
            continue
        for field in dataclasses.fields(candidate):
            if field.name in _NOT_A_CREDENTIAL:
                continue
            if _CREDENTIAL_SHAPED.search(field.name):
                offenders.append(f"{name}.{field.name}")
    assert offenders == []
