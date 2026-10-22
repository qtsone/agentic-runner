"""The contracts 3.31 wire for a person's Runner (QTS-2252, Spec: Runners rev 14).

What each side must agree on, and how a Runner on 3.31 still talks to a control plane on
3.30: the connections and the operator ride the bootstrap response and the ack only when
the control plane has them, and the device-flow login and ``connect`` bodies are strict.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from agentic_runner_contracts import __version__ as contracts_version
from agentic_runner_contracts.runner_login import (
    ConnectableOrganisations,
    DeviceAuthorization,
    DeviceLoginRequest,
    LoginResponse,
)
from agentic_runner_contracts.runner_registration import (
    BootstrapResponse,
    ContractLlmTerms,
    FloorState,
    HeartbeatAck,
    IsolationMode,
    RecipientKey,
    RunnerConnection,
    RunnerIdentityMaterial,
    SubscriptionRight,
)

ORGANISATION = UUID("00000000-0000-4000-8000-0000000000f1")
PERSON = UUID("00000000-0000-4000-8000-0000000000e1")


def _ack(**fields: Any) -> HeartbeatAck:
    return HeartbeatAck(
        runner_id=uuid4(),
        floor_state=FloorState.OK,
        contracts_floor=contracts_version,
        tag_set_version=1,
        accepts_new_directives=True,
        **fields,
    )


def _connection(**fields: Any) -> RunnerConnection:
    return RunnerConnection(
        organisation_id=ORGANISATION,
        temporal_namespace=f"org-{ORGANISATION}",
        task_queue="runner.x",
        **fields,
    )


def test_an_ack_names_connections_only_to_a_runner_it_has_them_for() -> None:
    # A Runner on 3.30 forbids the keys, so an ack from a control plane that has no
    # connections to name is the body that Runner already parses.
    assert "connections" not in _ack().model_dump(mode="json")
    assert "operator_user_profile_id" not in _ack().model_dump(mode="json")
    # An empty list still goes: connected nowhere is something to say.
    assert _ack(connections=[]).model_dump(mode="json")["connections"] == []

    body = _ack(
        operator_user_profile_id=PERSON,
        connections=[_connection(runner_token="t", paused=True)],
    ).model_dump(mode="json")

    parsed = HeartbeatAck.model_validate(body)
    assert parsed.operator_user_profile_id == PERSON
    assert parsed.connections is not None
    [connection] = parsed.connections
    assert (connection.organisation_id, connection.paused) == (ORGANISATION, True)


def test_a_bootstrap_response_from_an_older_control_plane_still_parses() -> None:
    response = BootstrapResponse(
        identity=RunnerIdentityMaterial(runner_id=uuid4(), identity_id="i", private_key_pem="k"),
        temporal_namespace="org-x",
        task_queue="runner.x",
        runner_token="t",
        runner_token_expires_at=datetime.now(UTC),
        tag_set_version=1,
        floor_state=FloorState.OK,
        contracts_floor=contracts_version,
    )

    body = response.model_dump(mode="json")

    assert "connections" not in body
    assert "operator_user_profile_id" not in body
    assert BootstrapResponse.model_validate(body).connections is None


def test_a_contracts_terms_name_their_organisation_only_when_there_is_one() -> None:
    bare = ContractLlmTerms(contract_id=uuid4(), subscription=SubscriptionRight.ALLOWED)
    named = bare.model_copy(update={"organisation_id": ORGANISATION, "organisation_name": "Acme"})

    assert "organisation_id" not in bare.model_dump(mode="json")
    assert ContractLlmTerms.model_validate(named.model_dump(mode="json")).organisation_name == (
        "Acme"
    )


def test_the_login_bodies_are_strict_and_carry_no_token() -> None:
    request = DeviceLoginRequest(
        name="laptop",
        isolation_mode=IsolationMode.NONE,
        contracts_version=contracts_version,
        runner_version="1.0.0",
        recipient_key=RecipientKey(key_id="key-0001", public_key="x" * 44),
    )
    assert "registration_token" not in request.model_dump()
    with pytest.raises(ValidationError):
        DeviceLoginRequest.model_validate(
            {**request.model_dump(mode="json"), "registration_token": "t" * 16}
        )
    with pytest.raises(ValidationError):
        DeviceLoginRequest.model_validate({**request.model_dump(mode="json"), "name": "Laptop!"})

    authorization = DeviceAuthorization(
        device_code="d" * 32,
        user_code="ABCD-EFGH",
        verification_uri="https://console.test/device",
        expires_in=600,
    )
    assert authorization.interval == 5

    login = LoginResponse(
        identity=RunnerIdentityMaterial(runner_id=uuid4(), identity_id="i", private_key_pem="k"),
        operator_user_profile_id=PERSON,
        name="laptop",
        task_queue="runner.x",
        floor_state=FloorState.OK,
        contracts_floor=contracts_version,
    )
    assert LoginResponse.model_validate(login.model_dump(mode="json")) == login


def test_connectable_organisations_say_why_one_is_refused() -> None:
    listed = ConnectableOrganisations.model_validate(
        {
            "organisations": [
                {"organisation_id": str(ORGANISATION), "name": "Acme", "connected": True},
                {
                    "organisation_id": str(uuid4()),
                    "name": "Other",
                    "refusal": "Members can connect their own Runners is off",
                },
            ]
        }
    )

    assert [o.refusal for o in listed.organisations] == [
        None,
        "Members can connect their own Runners is off",
    ]
