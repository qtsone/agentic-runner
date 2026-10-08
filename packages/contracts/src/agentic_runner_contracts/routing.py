"""Which Runner may run a Directive, as both sides state it (PRD issue 42).

Matching is **control-plane-side** (map ticket 25 §9; Buildkite matches server-side too,
26 §3): a platform activity picks the Runner and the workflow dispatches to
``runner.{runner_id}``. What travels from there is the decision itself -- the selector it
was matched on and the Contract's host party -- so the Runner can assert the same facts
before it acts (25 §9, 17 A3). That assertion is why this module lives in the *contracts*
distribution rather than in the platform: a stolen or misrouted task has to fail on the
Runner with the same vocabulary the router used, and a Runner cannot import the
platform.

Stdlib only, like :mod:`agentic_runner_contracts.activity_io`: ``DirectiveRouting`` rides
every Runner activity payload, so this module is imported into Temporal's determinism
sandbox with the workflow.

Three rules the shapes here carry:

* **Tags are exact values and presence, AND-ed, never globs** (25 §9). A glob would make
  "which Runners can see this Contract's workspace" a pattern-matching question, and an
  operator-typed pattern is exactly the free text ADR-0010 §6 keeps out of routing.
* **Host party is a routing rule, never a tag** (17 A3). It is fixed at registration from
  the Agent Token used, so it cannot be declared in config next to the tags; keeping it a
  separate field is what stops a contractor's laptop tagging itself into another
  contractor's work.
* **No credential is ever in here.** The Directive's own control-plane token is pulled by
  id over the heartbeat stream (25 §11), so nothing credential-shaped enters Temporal
  history; ``tests/unit/test_directive_routing_assertion.py`` is the structural gate.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final

# A selector entry with this value demands the *key*, whatever its value (25 §9's
# "presence"). The empty string is the marker rather than ``None`` so the selector stays
# a plain ``dict[str, str]`` across JSON, the wire and a JSON column.
PRESENT: Final = ""


def selector_satisfied(selector: Mapping[str, str], tags: Mapping[str, str]) -> bool:
    """Whether ``tags`` satisfies every entry of ``selector`` (25 §9: all keys AND-ed)."""

    return all(
        key in tags and (value == PRESENT or tags[key] == value) for key, value in selector.items()
    )


class SelectorConflictError(ValueError):
    """Two layers of the selector demand different values for the same tag key.

    Unsatisfiable by construction -- a tag holds one value -- so it is raised rather than
    resolved: silently letting the last layer win would route work to a Runner the
    Contract narrowed *away* from.
    """


def narrow_selector(*layers: Mapping[str, str]) -> dict[str, str]:
    """Merge the selector layers in narrowing order (25 §9).

    Product default, narrowed by the Contract, pinned by the Agent Runtime Profile: each
    layer may add a key or tighten a presence-only entry to an exact value, and may never
    loosen one. A layer naming a different exact value for a key an earlier layer pinned
    is a configuration contradiction, not a narrowing (:class:`SelectorConflictError`).
    """

    merged: dict[str, str] = {}
    for layer in layers:
        for key, value in layer.items():
            held = merged.get(key)
            if held is None or held in (PRESENT, value):
                merged[key] = value
            elif value != PRESENT:
                raise SelectorConflictError(
                    f"tag {key!r} is narrowed to {held!r} and to {value!r}; no Runner can be both"
                )
    return merged


@dataclass(frozen=True)
class DirectiveRouting:
    """The routing decision one Directive was dispatched by, carried in its payload.

    Present on every Runner activity input so the Runner can fail closed before any verb
    (25 §9): it re-checks the selector against its own Tags and the host party against
    its own registration, and refuses with Evidence when either disagrees. The Runner is
    told what it was matched *on*, never trusted to decide it.
    """

    runner_id: str
    # `organisation` | `account` | `user` -- the Contract's ``runner_host`` (17 A3).
    host_party: str
    selector: dict[str, str] = field(default_factory=dict)
    # The Contract the control plane admitted this Runner for (17 A2), and on a
    # wipe-and-reassign the departing one the wipe removes. `assert_routed` does not read
    # it: the single-Contract lock is held control-plane-side by `current_contract_id`.
    contract_id: str = ""


@dataclass(frozen=True)
class RunnerRoutingIdentity:
    """What a Runner process knows about itself, as the control plane registered it.

    Built at the composition root from the bootstrap response and the config-declared
    Tags -- never from an activity payload, which is the thing being checked.
    """

    runner_id: str
    host_party: str
    tags: dict[str, str] = field(default_factory=dict)


class RoutingRefusedError(RuntimeError):
    """A Runner refused a payload it was not routed for; ``reason`` is the stable code."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def assert_routed(routing: DirectiveRouting | None, identity: RunnerRoutingIdentity) -> None:
    """Fail closed unless this Runner is the one the control plane routed to (25 §9).

    Raised before the activity body runs, so a stolen or misrouted task costs no verb, no
    workspace and no LLM call. Retried on the same ``runner.{runner_id}`` queue, a
    persistent poller shows up in Evidence as the intra-Organisation incident it is
    (17 A11) rather than as silence.
    """

    if routing is None:
        raise RoutingRefusedError(
            "routing_absent",
            "activity payload carries no routing decision; this Runner cannot verify it "
            "was the one routed to",
        )
    if routing.runner_id != identity.runner_id:
        raise RoutingRefusedError(
            "runner_mismatch",
            f"payload was routed to Runner {routing.runner_id}, not to {identity.runner_id}",
        )
    if routing.host_party != identity.host_party:
        raise RoutingRefusedError(
            "host_party_mismatch",
            f"Contract requires a Runner hosted by {routing.host_party!r}; this Runner is "
            f"hosted by {identity.host_party!r}",
        )
    if not selector_satisfied(routing.selector, identity.tags):
        raise RoutingRefusedError(
            "selector_unsatisfied",
            f"this Runner's Tags do not satisfy the selector it was routed by "
            f"({sorted(routing.selector)})",
        )
