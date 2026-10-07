"""The failed-verification Incident's request shape (ADR-0013 §4).

The Runner reports a failed Verifier run and the platform opens the Incident, so the two
halves must agree on the payload. The key sets are the contract's enforcement edge: the
client rejects an unexpected or missing key before the request leaves the worker, which is
what keeps a renamed field from arriving at the backend as a silently dropped one.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, NotRequired, TypedDict

FAILED_VERIFIER_INCIDENT_REQUIRED_KEYS = frozenset({"reason"})
FAILED_VERIFIER_INCIDENT_ALLOWED_KEYS = FAILED_VERIFIER_INCIDENT_REQUIRED_KEYS | {"evidence"}


class FailedVerifierIncidentPayload(TypedDict):
    reason: str
    evidence: NotRequired[Mapping[str, Any]]
