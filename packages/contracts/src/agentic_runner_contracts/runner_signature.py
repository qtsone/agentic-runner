"""The signed Runner envelope: what a Runner signs and what the platform checks (issue 71).

A canonical-request signature, the shape of AWS SigV4 and RFC 9421: the signed string
names what the request *is* -- its method, its path with the query string, when it was
signed, and the SHA-256 of its exact body bytes -- not only what it carries. A body-only
signature let one captured Runner-scoped Evidence POST replay onto any other Work Record
of the Organisation, and one captured GET (every GET signs ``b""``) stand in for every
Runner-scoped GET, forever.

Both sides build the string here, so the Runner's signer and the platform's verifier
cannot drift. The platform rebuilds it from the request it *received*, never from a value
the Runner claims. There is no nonce store: a replay inside the window, to the same method
and path, is accepted (heartbeat usage is idempotent on ``(directive_id, sequence)``).
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Final

RUNNER_ID_HEADER: Final = "X-Runner-Id"
SIGNATURE_HEADER: Final = "X-Runner-Signature"
# Unix seconds, as a decimal string. A signed request without it is a pre-1.0 Runner.
SIGNED_AT_HEADER: Final = "X-Runner-Signed-At"

# ±5 minutes around the platform's clock: tolerates workstation drift, matches SigV4.
SIGNATURE_MAX_SKEW: Final = timedelta(minutes=5)


def format_signed_at(moment: datetime) -> str:
    return str(int(moment.timestamp()))


def parse_signed_at(value: str) -> datetime:
    """Raises ``ValueError`` on anything but a plain decimal Unix-seconds value."""

    if not value.isascii() or not value.isdigit() or len(value) > 12:
        raise ValueError(f"{SIGNED_AT_HEADER} is not Unix seconds: {value!r}")
    return datetime.fromtimestamp(int(value), UTC)


def canonical_request(*, method: str, path: str, signed_at: str, body: bytes) -> bytes:
    """The exact bytes a Runner signs. ``path`` is the raw request target, query included.

    Newline-separated so no field can run into the next: none of the four may carry one
    (an HTTP method and request target cannot, the other two are digits and hex).
    """

    return "\n".join((method.upper(), path, signed_at, hashlib.sha256(body).hexdigest())).encode(
        "utf-8"
    )
