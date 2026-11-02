"""Secrets released at Directive start (Spec: Agent Guardrails rev 7, S4, A2).

A Credential Reference the Runner's host does not hold is asked of the control plane as a
Secret, by a Runner holding the Organisation Key. The host's own value under the same name
still counts and wins, a refusal or a release that does not open fails the Directive
closed with Evidence naming the reference, and a value lives only in that Directive's
credentials.
"""

from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path
from typing import Any

import pytest

from agentic_runner.activities import RunnerRalphActivities, _RuntimeContextState
from agentic_runner.credentials import (
    CREDENTIAL_UNRESOLVABLE_SOURCE,
    CredentialResolver,
    FakeCredentialStore,
    UnresolvableCredentialReferenceError,
)
from agentic_runner.pool_key import LlmKeyOpener, LlmKeyUnopenableError, parse_pool_key
from agentic_runner.sealed_box import RecipientKeyPair, RecipientKeyStore
from agentic_runner.workers.fastapi_client import WorkerFastApiClientRejectionError
from agentic_runner_contracts.secret import SecretRelease

CONTRACT = "5f0b8c2e-1d3a-4e6f-9a7b-2c4d6e8f0a1b"


def _fixture() -> dict[str, str]:
    raw = files("agentic_runner_contracts").joinpath("fixtures/secret.fixture.json")
    return dict(json.loads(raw.read_text(encoding="utf-8")))


def _release_body(fixture: dict[str, str]) -> dict[str, Any]:
    return {
        "organisation_id": fixture["organisation_id"],
        "recipient_key_id": fixture["recipient_key_id"],
        "secrets": [
            {
                "secret_id": fixture["secret_id"],
                "name": fixture["name"],
                "scope": fixture["scope"],
                "holder_kind": fixture["holder_kind"],
                "holder_fingerprint": fixture["holder_fingerprint"],
                "holder_share": fixture["holder_share"],
                "platform_share": fixture["platform_share"],
            }
        ],
    }


def _opener(tmp_path: Path, fixture: dict[str, str], *, pool_key: bool = True) -> LlmKeyOpener:
    keys = RecipientKeyStore(tmp_path)
    pair = RecipientKeyPair(
        key_id=fixture["recipient_key_id"],
        public_key=fixture["recipient_public_key"],
        private_key=fixture["recipient_private_key"],
    )
    keys.install(pair, managed_by="test", now=keys.rotated_at())
    return LlmKeyOpener(
        pool_key=parse_pool_key(fixture["organisation_key_private"]) if pool_key else None,
        recipient_keys=keys,
    )


class _ControlPlane:
    def __init__(self, answer: dict[str, Any] | Exception) -> None:
        self.answer = answer
        self.asked: list[dict[str, Any]] = []
        self.appended: list[tuple[str, dict[str, Any]]] = []

    async def release_secrets(self, work_record_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.asked.append(payload)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer

    async def append_evidence(
        self, work_record_id: str, *, source: str, payload: dict[str, Any]
    ) -> None:
        self.appended.append((source, payload))


def _activities(
    control_plane: _ControlPlane, opener: LlmKeyOpener, host: dict[str, str] | None = None
) -> RunnerRalphActivities:
    return RunnerRalphActivities(
        control_plane,  # type: ignore[arg-type]
        credentials=CredentialResolver(store=FakeCredentialStore(host or {})),
        llm_keys=opener,
    )


def _state(*references: str) -> Any:
    return _RuntimeContextState(
        reviewer=None,
        repository="acme/repo",
        verifier_argv=("true",),
        work_branch="agent/work",
        completion_criteria="do the work",
        contract_id=CONTRACT,
        credential_references=references,
    )


@pytest.mark.asyncio
async def test_a_reference_the_host_lacks_is_released_and_opened(tmp_path: Path) -> None:
    fixture = _fixture()
    control_plane = _ControlPlane(_release_body(fixture))
    activities = _activities(control_plane, _opener(tmp_path, fixture), {"GH_TOKEN": "host's"})

    credentials = await activities._resolve_credentials(
        "wr-1", _state("GH_TOKEN", fixture["name"]), "wr-1:1"
    )

    assert credentials is not None
    assert credentials.for_verb_seam(fixture["name"]) == fixture["value"]
    assert credentials.for_verb_seam("GH_TOKEN") == "host's"
    assert control_plane.asked == [
        {
            "directive_id": "wr-1:1",
            "names": [fixture["name"]],
            "holder_fingerprints": [fixture["holder_fingerprint"]],
        }
    ]


@pytest.mark.asyncio
async def test_the_hosts_own_value_wins_and_nothing_is_asked(tmp_path: Path) -> None:
    fixture = _fixture()
    control_plane = _ControlPlane(_release_body(fixture))
    activities = _activities(
        control_plane, _opener(tmp_path, fixture), {fixture["name"]: "the host's own"}
    )

    credentials = await activities._resolve_credentials("wr-1", _state(fixture["name"]), "wr-1:1")

    assert credentials is not None
    assert credentials.for_verb_seam(fixture["name"]) == "the host's own"
    assert control_plane.asked == []


@pytest.mark.asyncio
async def test_a_runner_without_the_organisation_key_asks_nothing(tmp_path: Path) -> None:
    fixture = _fixture()
    control_plane = _ControlPlane(_release_body(fixture))
    activities = _activities(control_plane, _opener(tmp_path, fixture, pool_key=False))

    with pytest.raises(UnresolvableCredentialReferenceError) as refusal:
        await activities._resolve_credentials("wr-1", _state(fixture["name"]), "wr-1:1")

    assert refusal.value.reason == "not_installed"
    assert control_plane.asked == []


@pytest.mark.asyncio
async def test_a_refused_release_fails_the_directive_closed_with_evidence(tmp_path: Path) -> None:
    fixture = _fixture()
    refused = WorkerFastApiClientRejectionError("refused", status_code=403)
    control_plane = _ControlPlane(refused)
    activities = _activities(control_plane, _opener(tmp_path, fixture))

    with pytest.raises(UnresolvableCredentialReferenceError):
        await activities._resolve_credentials("wr-1", _state(fixture["name"]), "wr-1:1")

    assert control_plane.appended == [
        (
            CREDENTIAL_UNRESOLVABLE_SOURCE,
            {
                "event": "credential_reference.unresolvable",
                "reference": fixture["name"],
                "contract_id": CONTRACT,
                "reason": "secret_release_refused:403",
            },
        )
    ]


@pytest.mark.asyncio
async def test_a_release_that_does_not_open_fails_closed_and_names_no_value(
    tmp_path: Path,
) -> None:
    fixture = _fixture()
    body = _release_body(fixture)
    body["secrets"][0]["secret_id"] = "0e5f8a31-9b2c-4d7e-a610-3c8b1f4d2e95"
    control_plane = _ControlPlane(body)
    activities = _activities(control_plane, _opener(tmp_path, fixture))

    with pytest.raises(UnresolvableCredentialReferenceError) as refusal:
        await activities._resolve_credentials("wr-1", _state(fixture["name"]), "wr-1:1")

    assert refusal.value.reason == "secret_unopenable"
    assert fixture["value"] not in json.dumps(control_plane.appended)


def test_a_release_carrying_a_name_not_asked_for_does_not_open(tmp_path: Path) -> None:
    fixture = _fixture()
    release = SecretRelease.model_validate(_release_body(fixture))

    with pytest.raises(LlmKeyUnopenableError):
        _opener(tmp_path, fixture).open_secrets(release, names=["SOMETHING_ELSE"])
    assert _opener(tmp_path, fixture).open_secrets(release, names=[fixture["name"]]) == {
        fixture["name"]: fixture["value"]
    }
