"""The OAuth Connector items on the wire (console-v2 issue 30, contracts 3.8).

* every new item round-trips through JSON, on the ack and on the envelope;
* an ack and an envelope from contracts 3.7, without any OAuth key, still parse;
* no item has a field a verifier, a plaintext code or a plaintext token could ride in;
* every endpoint is HTTPS.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import pytest
from pydantic import BaseModel, ValidationError

from agentic_runner_contracts import oauth_connector
from agentic_runner_contracts.oauth_connector import (
    OAuthAuthorizeUrl,
    OAuthClient,
    OAuthDisconnect,
    OAuthOutcome,
    OAuthOutcomeKind,
    OAuthStart,
    SealedOAuthCode,
    oauth_code_binding,
)
from agentic_runner_contracts.runner_registration import HeartbeatAck, HeartbeatEnvelope
from agentic_runner_contracts.sealed_credential import (
    SealedCredential,
    delivery_binding,
    sign_in_code_binding,
)

CONTRACT = uuid4()
AUTHORIZATION = "oa_" + "c" * 32

CLIENT = OAuthClient(
    authorize_endpoint="https://provider.test/authorize?tenant=t1",
    token_endpoint="https://provider.test/token",
    revocation_endpoint="https://provider.test/revoke",
    client_id="client-123",
    client_secret_slot="LINEAR_CLIENT_SECRET",
    scopes=["read", "issues:write"],
)

ACK_3_7: dict[str, Any] = {
    "runner_id": str(uuid4()),
    "floor_state": "ok",
    "contracts_floor": "3.7.0",
    "tag_set_version": 1,
    "accepts_new_directives": True,
}

ENVELOPE_3_7: dict[str, Any] = {
    "runner_version": "3.7.0",
    "contracts_version": "3.7.0",
    "resource_pressure": {"cpu_percent": 1.0, "memory_percent": 1.0, "disk_percent": 1.0},
    "max_concurrent_directives": 1,
    "current_load": 0,
    "egress_posture": "unrestricted",
    "isolation_mode": "none",
    "tag_set_version": 1,
    "hosted_task_queue": "runner.x",
}


def _ack_items() -> dict[str, Any]:
    return {
        "oauth_starts": [
            OAuthStart(
                contract_id=CONTRACT,
                slot="LINEAR_TOKEN",
                authorization_id=AUTHORIZATION,
                client=CLIENT,
                redirect_uri="https://agentic.example/connectors/oauth/callback",
            )
        ],
        "oauth_codes": [
            SealedOAuthCode(
                contract_id=CONTRACT,
                authorization_id=AUTHORIZATION,
                recipient_key_id="rk_" + "0" * 32,
                state="nonce",
                ciphertext="AAAA",
            )
        ],
        "oauth_disconnects": [
            OAuthDisconnect(contract_id=CONTRACT, slot="LINEAR_TOKEN", client=CLIENT)
        ],
    }


def _envelope_items() -> dict[str, Any]:
    return {
        "oauth_authorizations": [
            OAuthAuthorizeUrl(
                contract_id=CONTRACT,
                slot="LINEAR_TOKEN",
                authorization_id=AUTHORIZATION,
                authorize_url="https://provider.test/authorize?code_challenge=x&state=y",
            )
        ],
        "oauth_tokens": [
            SealedCredential(
                contract_id=CONTRACT,
                slot="LINEAR_TOKEN",
                recipient_key_id="rk_" + "0" * 32,
                version=2,
                ciphertext="AAAA",
            )
        ],
        "oauth_outcomes": [
            OAuthOutcome(
                contract_id=CONTRACT,
                slot="LINEAR_TOKEN",
                outcome=OAuthOutcomeKind.REFRESH_FAILED,
                provider_error="invalid_grant",
            )
        ],
    }


def test_the_ack_items_round_trip() -> None:
    ack = HeartbeatAck.model_validate({**ACK_3_7, **_ack_items()})

    assert HeartbeatAck.model_validate_json(ack.model_dump_json()) == ack


def test_the_envelope_items_round_trip() -> None:
    envelope = HeartbeatEnvelope.model_validate({**ENVELOPE_3_7, **_envelope_items()})

    assert HeartbeatEnvelope.model_validate_json(envelope.model_dump_json()) == envelope


def test_a_3_7_ack_and_envelope_still_parse_and_an_empty_envelope_omits_the_keys() -> None:
    ack = HeartbeatAck.model_validate(ACK_3_7)
    envelope = HeartbeatEnvelope.model_validate(ENVELOPE_3_7)

    assert (ack.oauth_starts, ack.oauth_codes, ack.oauth_disconnects) == ([], [], [])
    body = json.loads(envelope.model_dump_json())
    assert not {"oauth_authorizations", "oauth_tokens", "oauth_outcomes"} & set(body)


def _fields(model: type[BaseModel]) -> set[str]:
    names = set(model.model_fields)
    for info in model.model_fields.values():
        if isinstance(info.annotation, type) and issubclass(info.annotation, BaseModel):
            names |= _fields(info.annotation)
    return names


@pytest.mark.parametrize(
    "model",
    [OAuthStart, OAuthAuthorizeUrl, SealedOAuthCode, OAuthDisconnect, OAuthOutcome],
)
def test_no_item_has_a_field_for_the_verifier_a_code_or_a_token(model: type[BaseModel]) -> None:
    forbidden = {
        "verifier",
        "code_verifier",
        "code",
        "access_token",
        "refresh_token",
        "token",
        "client_secret",
    }

    assert not _fields(model) & forbidden


@pytest.mark.parametrize("field", ["authorize_endpoint", "token_endpoint", "revocation_endpoint"])
def test_an_endpoint_must_be_https(field: str) -> None:
    with pytest.raises(ValidationError):
        OAuthClient.model_validate({**CLIENT.model_dump(), field: "http://provider.test/" + field})


def test_the_code_binding_is_its_own_domain() -> None:
    triple = {"contract_id": CONTRACT, "recipient_key_id": "rk_" + "0" * 32}

    code = oauth_code_binding(authorization_id=AUTHORIZATION, **triple)

    assert code.startswith(oauth_connector.OAUTH_CODE_DOMAIN.encode())
    assert code != sign_in_code_binding(sign_in_id=AUTHORIZATION, **triple)
    assert code != delivery_binding(slot=AUTHORIZATION, **triple)


def test_a_provider_error_that_is_not_a_code_is_refused() -> None:
    with pytest.raises(ValidationError):
        OAuthOutcome(
            contract_id=CONTRACT,
            outcome=OAuthOutcomeKind.EXCHANGE_FAILED,
            provider_error="invalid_grant: code at-123 expired",
        )
