"""Which auth mode one Directive runs in, decided on the Runner before spawn (local-agents 04).

Subscriptions follow the person, not the host (owner, 2026-09-26): a harness runs on a
subscription login only on the Contract person's own Runner -- host party ``user``, which
routing has already tied to the Contract's person (``assert_routed`` here, the platform's
``_subscription_bound`` there) -- whatever channel the Runner was installed by. A shared
Runner (``organisation`` or ``account``) runs every Directive through the LLM proxy on an
API key and refuses sign-in. The platform refuses an ineligible sign-in at the door
(local-agents 03); this module is the backstop, so it decides from what the Runner itself
knows and never from the payload.
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Final

from agentic_runner.workers.agent_runtime import AuthMode

__all__ = [
    "SETUP_TOKEN_REFUSED_RULE",
    "SHARED_HOST_PARTIES",
    "SHARED_RUNNER_SIGN_IN_RULE",
    "SHARED_RUNNER_SUBSCRIPTION_RULE",
    "AuthModeRefusedError",
    "choose_auth_mode",
    "is_setup_token",
    "is_shared_runner",
]

SHARED_HOST_PARTIES: Final[frozenset[str]] = frozenset({"organisation", "account"})

# The rule names a refusal's Evidence carries, so a support read can say which line held.
SHARED_RUNNER_SUBSCRIPTION_RULE: Final[str] = "shared_runner_api_key_only"
SHARED_RUNNER_SIGN_IN_RULE: Final[str] = "shared_runner_sign_in_refused"
SETUP_TOKEN_REFUSED_RULE: Final[str] = "setup_token_refused"

# `claude setup-token` mints an OAuth bearer under this prefix; an Anthropic API key is
# `sk-ant-api…`. The sealed wire carries no mode, so the value's own shape is what the
# Runner can refuse on.
_SETUP_TOKEN_PREFIX: Final[str] = "sk-ant-oat"


class AuthModeRefusedError(RuntimeError):
    """A Directive or sign-in this Runner will not run; ``rule`` is the stable code."""

    def __init__(self, rule: str, message: str) -> None:
        super().__init__(message)
        self.rule = rule


def is_shared_runner(host_party: str | None) -> bool:
    return host_party in SHARED_HOST_PARTIES


def choose_auth_mode(
    *,
    host_party: str | None,
    key_present: bool,
    runtime_modes: Collection[AuthMode],
) -> AuthMode:
    """``api_key`` whenever a key is present; otherwise ``subscription`` on a user-hosted
    Runner whose runtime can run one, and a refusal on a shared Runner.

    The refusal is the shared Runner's "subscription case": with no key there is nothing
    for the proxy to spend, and the only thing left that could authenticate the harness is
    a login, which a shared Runner never uses. An unregistered process (``host_party``
    ``None``) is not user-hosted either, so it never runs a subscription; it runs on a key
    or fails at the proxy for want of one, as it would today.
    """

    if key_present:
        return AuthMode.API_KEY
    if is_shared_runner(host_party):
        raise AuthModeRefusedError(
            SHARED_RUNNER_SUBSCRIPTION_RULE,
            f"this Runner is hosted by {host_party!r}, so it runs API keys only, and no API "
            "key is present for this Contract",
        )
    if host_party == "user" and AuthMode.SUBSCRIPTION in runtime_modes:
        return AuthMode.SUBSCRIPTION
    return AuthMode.API_KEY


def is_setup_token(value: str) -> bool:
    return value.strip().startswith(_SETUP_TOKEN_PREFIX)
