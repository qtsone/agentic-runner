"""The Guardrails' contract: the model, the posture on the heartbeat, and 3.x bodies.

The Runner's enforcement is ``test_guardrails_enforcement.py``'s.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agentic_runner_contracts.guardrails import (
    GuardrailLayer,
    GuardrailLimitName,
    GuardrailPart,
    Guardrails,
    describe_refusal,
    refusal_evidence,
)
from agentic_runner_contracts.runner_registration import (
    BootstrapRequest,
    EgressPosture,
    GuardrailPosture,
    GuardrailsPosture,
    HeartbeatEnvelope,
    IsolationMode,
    RecipientKey,
    ResourcePressure,
)

COMPILED = {
    "commands": ["pytest", "npm test"],
    "network": ["*.example.com", "db.example:5432"],
    "folders": ["src"],
    "limits": {
        "memory_limit_bytes": {"value": 1 << 30, "layer": "organisation"},
        "wall_clock_seconds": {"value": 600, "layer": "team"},
    },
    "permission_fallback": "hold",
    "removed": [{"part": "network", "entry": "db.prod.internal", "layer": "product"}],
}
POSTURE = GuardrailsPosture(
    network=EgressPosture.UNENFORCED,
    commands={"claude_code": GuardrailPosture.CHECKED},
    folders={"claude_code": GuardrailPosture.CHECKED, "codex_cli": GuardrailPosture.UNENFORCED},
    memory=GuardrailPosture.UNENFORCED,
)


def test_the_compiled_guardrails_round_trip() -> None:
    guardrails = Guardrails.model_validate(COMPILED)

    assert Guardrails.model_validate_json(guardrails.model_dump_json()) == guardrails
    assert guardrails.limit(GuardrailLimitName.WALL_CLOCK_SECONDS) is not None
    assert [r.layer for r in guardrails.removed_from(GuardrailPart.NETWORK)] == [
        GuardrailLayer.PRODUCT
    ]
    assert guardrails.removed_from(GuardrailPart.FOLDERS) == ()


def test_empty_guardrails_are_the_runners_defaults() -> None:
    assert Guardrails() == Guardrails.model_validate({})


@pytest.mark.parametrize(
    "bad",
    [
        {**COMPILED, "limits": {"cpu_seconds": {"value": 1, "layer": "organisation"}}},
        {**COMPILED, "limits": {"wall_clock_seconds": {"value": 0, "layer": "organisation"}}},
        {**COMPILED, "removed": [{"part": "limits", "entry": "x", "layer": "product"}]},
        {**COMPILED, "removed": [{"part": "network", "entry": "x", "layer": "platform"}]},
        {**COMPILED, "permission_fallback": "allow"},
        {**COMPILED, "profile_slug": "engineer-codex-default"},
    ],
)
def test_a_body_the_contract_does_not_know_is_refused(bad: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Guardrails.model_validate(bad)


def test_a_refusal_reads_as_the_console_shows_it() -> None:
    summary = describe_refusal(
        part=GuardrailPart.NETWORK, entry="db.prod.internal", layer=GuardrailLayer.PRODUCT
    )
    evidence = refusal_evidence(
        part=GuardrailPart.NETWORK,
        entry="db.prod.internal",
        layer=GuardrailLayer.PRODUCT,
        port=5432,
    )

    assert summary == "Refused by the Product's Guardrails: network `db.prod.internal`"
    assert evidence == {
        "event": "guardrails.refused",
        "part": "network",
        "entry": "db.prod.internal",
        "layer": "product",
        "summary": summary,
        "port": 5432,
    }


def _envelope(**fields: object) -> HeartbeatEnvelope:
    return HeartbeatEnvelope.model_validate(
        {
            "runner_version": "4.0.0",
            "contracts_version": "4.0.0",
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


def test_the_heartbeat_round_trips_the_posture() -> None:
    sent = _envelope(guardrails_posture=POSTURE)

    received = HeartbeatEnvelope.model_validate_json(sent.model_dump_json())

    assert received.guardrails_posture == POSTURE
    assert received.guardrails_posture.wall_clock is GuardrailPosture.CHECKED


RUNNER_LIMIT_3X = {
    "limit": {"tools": [], "networks": ["evil.example"], "folders": ["/srv"]},
    "posture": {"tools": "enforced", "networks": "unenforced", "folders": {}},
}


def test_a_3x_beat_with_a_runner_limit_parses_without_it_so_the_floor_can_hold_it() -> None:
    envelope = _envelope(contracts_version="3.34.0", runner_limit=RUNNER_LIMIT_3X)

    assert envelope.guardrails_posture is None
    assert "runner_limit" not in envelope.model_dump()


def test_a_3x_bootstrap_with_a_runner_limit_parses_without_it() -> None:
    request = BootstrapRequest.model_validate(
        {
            "registration_token": "registration-token-of-sixteen-plus",
            "isolation_mode": "none",
            "contracts_version": "3.34.0",
            "runner_version": "3.34.0",
            "recipient_key": RecipientKey(key_id="key-0001", public_key="k" * 32).model_dump(),
            "runner_limit": RUNNER_LIMIT_3X,
        }
    )

    assert "runner_limit" not in request.model_dump()


def test_a_posture_the_contract_does_not_know_is_refused() -> None:
    with pytest.raises(ValidationError):
        GuardrailsPosture.model_validate({"network": "unenforced", "memory": "enforced"})
    with pytest.raises(ValidationError):
        GuardrailsPosture.model_validate(
            {"network": "unenforced", "memory": "checked", "wall_clock": "unenforced"}
        )
