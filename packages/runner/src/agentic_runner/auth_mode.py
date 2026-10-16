"""Which auth mode one Directive runs in, decided on the Runner before spawn (local-agents 04).

Subscriptions follow the person and the actual operator, not the host party (QTS-1349,
QTS-1450). A harness runs on a subscription login where the Runner can verify that the
login is the Contract's alone:

* on the Contract person's own Runner -- host party ``user``, which routing has already
  tied to the Contract's person (``assert_routed`` here, the platform's
  ``_subscription_bound`` there);
* on a shared Runner (``organisation`` or ``account``, e.g. a customer-operated cluster)
  only when the Directive runs as its Contract's own uid and that Contract's harness root
  holds a login. The uid is what keeps one Contract's login out of every other
  Contract's reach on the same host; the login is what the person made there themselves.

The platform decides who may use a subscription at the door (the Subscription Principal
and the Organisation's opt-in, QTS-1350); this module is the backstop, so it decides from
what the Runner itself can check and never from the payload.

From contracts 3.25 the platform names the access in the Directive's routing
(``DirectiveRouting.llm_access``, Spec: Runners rev 3). :func:`require_named_access` then
replaces the choice: the Runner runs on exactly the named access or refuses, and never
falls back to another. A Directive without it, from a control plane on an older pin, keeps
:func:`choose_auth_mode`.
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Final

from agentic_runner.workers.agent_runtime import AuthMode
from agentic_runner_contracts.llm_access import LlmAccess, LlmAccessKind
from agentic_runner_contracts.runner_registration import SubscriptionRight, SubscriptionState

__all__ = [
    "NO_RUNTIME_SUBSCRIPTION_RULE",
    "HOST_KEY_NOT_HELD_RULE",
    "LLM_ACCESS_UNKNOWN_RULE",
    "LLM_KEY_RELEASE_REFUSED_RULE",
    "LLM_KEY_UNOPENABLE_RULE",
    "POOL_KEY_NOT_HELD_RULE",
    "SETUP_TOKEN_REFUSED_RULE",
    "SUBSCRIPTION_NOT_ACKNOWLEDGED_RULE",
    "SUBSCRIPTION_NOT_SIGNED_IN_RULE",
    "SUBSCRIPTION_NO_RIGHT_RULE",
    "SUBSCRIPTION_OFF_PERSONAL_RUNNER_RULE",
    "SHARED_HOST_PARTIES",
    "SIGN_IN_NEEDS_CONTRACT_UID_RULE",
    "SUBSCRIPTION_NEEDS_CONTRACT_UID_RULE",
    "SUBSCRIPTION_NEEDS_HARNESS_LOGIN_RULE",
    "AuthModeRefusedError",
    "choose_auth_mode",
    "require_named_access",
    "is_setup_token",
    "is_shared_runner",
    "runs_subscriptions",
]

SHARED_HOST_PARTIES: Final[frozenset[str]] = frozenset({"organisation", "account"})

# The rule names a refusal's Evidence carries, so a support read can say which line held.
SUBSCRIPTION_NEEDS_CONTRACT_UID_RULE: Final[str] = "subscription_needs_contract_uid"
SUBSCRIPTION_NEEDS_HARNESS_LOGIN_RULE: Final[str] = "subscription_needs_harness_login"
NO_RUNTIME_SUBSCRIPTION_RULE: Final[str] = "runtime_has_no_subscription_mode"
SIGN_IN_NEEDS_CONTRACT_UID_RULE: Final[str] = "sign_in_needs_contract_uid"
SETUP_TOKEN_REFUSED_RULE: Final[str] = "setup_token_refused"
# A Directive whose named `llm_access` this Runner no longer holds (rev 3).
HOST_KEY_NOT_HELD_RULE: Final[str] = "host_key_not_held"
SUBSCRIPTION_NOT_SIGNED_IN_RULE: Final[str] = "subscription_not_signed_in"
SUBSCRIPTION_OFF_PERSONAL_RUNNER_RULE: Final[str] = "subscription_off_personal_runner"
SUBSCRIPTION_NO_RIGHT_RULE: Final[str] = "subscription_no_right"
SUBSCRIPTION_NOT_ACKNOWLEDGED_RULE: Final[str] = "subscription_not_acknowledged"
# An `llm_key` Directive on a Runner given no Pool Key; one whose release QTS refused; and
# one whose release does not open here (an old Pool Key, or a share bound to another key).
POOL_KEY_NOT_HELD_RULE: Final[str] = "pool_key_not_held"
LLM_KEY_RELEASE_REFUSED_RULE: Final[str] = "llm_key_release_refused"
LLM_KEY_UNOPENABLE_RULE: Final[str] = "llm_key_unopenable"
LLM_ACCESS_UNKNOWN_RULE: Final[str] = "llm_access_unknown"
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


def runs_subscriptions(host_party: str | None, *, separates_contract_uids: bool) -> bool:
    """Whether a subscription login made on this Runner could ever run a Directive.

    What a sign-in is checked against: one made where no Directive could use it would
    only leave a login lying in a harness root. An unregistered process (no host party)
    makes no routing assertion, so it is nobody's own Runner and never qualifies.
    """

    return host_party == "user" or (is_shared_runner(host_party) and separates_contract_uids)


def choose_auth_mode(
    *,
    host_party: str | None,
    key_present: bool,
    runtime_modes: Collection[AuthMode],
    contract_uid: bool,
    harness_login: bool,
) -> AuthMode:
    """``api_key`` whenever a key is present; otherwise ``subscription`` where the Runner
    can verify the login is this Contract's, and a refusal on a shared Runner where it
    cannot.

    ``contract_uid`` says the Directive runs as its own Contract's uid (never the shared
    no-Contract tree's); ``harness_login`` that this Contract's harness root for the
    Directive's kind holds a login. A shared Runner with no key and either one missing has
    nothing left that could authenticate the harness, so it refuses, naming the missing
    one. An unregistered process (``host_party`` ``None``) never runs a subscription; it
    runs on a key or fails at the proxy for want of one.
    """

    if key_present:
        return AuthMode.API_KEY
    if host_party == "user":
        return AuthMode.SUBSCRIPTION if AuthMode.SUBSCRIPTION in runtime_modes else AuthMode.API_KEY
    if not is_shared_runner(host_party):
        return AuthMode.API_KEY
    if not contract_uid:
        raise AuthModeRefusedError(
            SUBSCRIPTION_NEEDS_CONTRACT_UID_RULE,
            f"this Runner is hosted by {host_party!r} and does not run this Contract under its "
            "own uid, so it runs API keys only, and no API key is present for this Contract",
        )
    if not harness_login:
        raise AuthModeRefusedError(
            SUBSCRIPTION_NEEDS_HARNESS_LOGIN_RULE,
            "no API key is present for this Contract and its harness root on this Runner holds "
            "no login; sign it in on this Runner with `agentic-runner sign-in`",
        )
    if AuthMode.SUBSCRIPTION not in runtime_modes:
        raise AuthModeRefusedError(
            NO_RUNTIME_SUBSCRIPTION_RULE,
            "no API key is present for this Contract and this Runner's runtime for its CLI "
            "kind runs on API keys only",
        )
    return AuthMode.SUBSCRIPTION


def require_named_access(
    access: LlmAccess,
    *,
    host_party: str | None,
    host_key_held: bool,
    subscription: SubscriptionState,
    subscription_right: SubscriptionRight | None,
    runtime_modes: Collection[AuthMode],
    pool_key_held: bool = False,
) -> AuthMode:
    """The mode the named access runs in, or a refusal naming what is missing.

    ``host_key_held`` is the provider's key in this Runner's own store, serving in the
    proxy; ``subscription`` the Contract's harness root for the Directive's kind;
    ``pool_key_held`` whether this Runner was given a Pool Key, without which none of the
    pool's LLM Keys opens here. A
    subscription runs only on its person's own Runner (rev 3: never on a pool Runner),
    and only while it is signed in, unexpired and not at its usage limit.

    ``subscription_right`` is what the last heartbeat ack said of the Contract's own
    subscription (``llm_contracts``), so a misrouted Directive is refused here as well as
    at the platform's routing (QTS-1349 controls 4 and 7, QTS-1751). ``None`` while no ack
    has named the Contracts -- a control plane below 3.25 -- which leaves routing the only
    check, as before.
    """

    if access.kind == LlmAccessKind.HOST_KEY:
        if not host_key_held:
            raise AuthModeRefusedError(
                HOST_KEY_NOT_HELD_RULE,
                "this Directive was routed to the provider's key in this Runner's own store, "
                "and the store no longer holds a key the provider accepts",
            )
        return AuthMode.API_KEY
    if access.kind == LlmAccessKind.SUBSCRIPTION:
        if host_party != "user":
            raise AuthModeRefusedError(
                SUBSCRIPTION_OFF_PERSONAL_RUNNER_RULE,
                f"this Runner is hosted by {host_party!r}; a subscription runs only on its "
                "person's own Runner",
            )
        if subscription_right == SubscriptionRight.NO_RIGHT:
            raise AuthModeRefusedError(
                SUBSCRIPTION_NO_RIGHT_RULE,
                "this Directive was routed to the person's subscription, and the control plane "
                "last told this Runner that the Contract does not allow one",
            )
        if subscription_right == SubscriptionRight.NOT_ACKNOWLEDGED:
            raise AuthModeRefusedError(
                SUBSCRIPTION_NOT_ACKNOWLEDGED_RULE,
                "this Directive was routed to the person's subscription, and the control plane "
                "last told this Runner that the person has not acknowledged the subscription "
                "terms",
            )
        if AuthMode.SUBSCRIPTION not in runtime_modes:
            raise AuthModeRefusedError(
                NO_RUNTIME_SUBSCRIPTION_RULE,
                "this Runner's runtime for the Directive's CLI kind runs on API keys only",
            )
        if subscription != SubscriptionState.SIGNED_IN:
            raise AuthModeRefusedError(
                SUBSCRIPTION_NOT_SIGNED_IN_RULE,
                f"this Directive was routed to the person's subscription, and its sign-in on "
                f"this Runner is {subscription.value}; run `agentic-runner sign-in` here",
            )
        return AuthMode.SUBSCRIPTION
    if access.kind == LlmAccessKind.LLM_KEY:
        if not pool_key_held:
            raise AuthModeRefusedError(
                POOL_KEY_NOT_HELD_RULE,
                "this Directive was routed to one of the pool's LLM Keys, and this Runner "
                "holds no Pool Key; give it the one on the Runners page as "
                "AGENTIC_RUNNER_POOL_KEY",
            )
        return AuthMode.API_KEY
    raise AuthModeRefusedError(
        LLM_ACCESS_UNKNOWN_RULE, f"this Runner does not know the LLM access {access.kind!r}"
    )


def is_setup_token(value: str) -> bool:
    return value.strip().startswith(_SETUP_TOKEN_PREFIX)
