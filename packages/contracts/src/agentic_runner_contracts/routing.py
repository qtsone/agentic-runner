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

* **A selector is Kubernetes' ``matchExpressions``, never globs** (ADR-0022 §22, 25 §9):
  requirements over exact values, all of which must hold. A glob would make "which Runners
  can see this Contract's workspace" a pattern-matching question, and an operator-typed
  pattern is exactly the free text ADR-0010 §6 keeps out of routing.
* **Host party is a routing rule, never a tag** (17 A3). It is fixed at registration from
  the Runner Registration Token used, so it cannot be declared in config next to the tags;
  keeping it a separate field is what stops a contractor's laptop tagging itself into another
  contractor's work.
* **No credential is ever in here.** The Directive's own control-plane token is pulled by
  id over the heartbeat stream (25 §11), so nothing credential-shaped enters Temporal
  history; ``tests/unit/test_directive_routing_assertion.py`` is the structural gate.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final

from agentic_runner_contracts.llm_access import LlmAccess

# In a legacy ``{key: value}`` selector, this value demanded the *key*, whatever its value
# (25 §9's "presence"); :func:`selector_from_legacy` reads it as ``Exists``.
PRESENT: Final = ""


class SelectorOperator(StrEnum):
    """Kubernetes' set operators, spelled as Kubernetes spells them (ADR-0022 §22)."""

    IN = "In"
    NOT_IN = "NotIn"
    EXISTS = "Exists"
    DOES_NOT_EXIST = "DoesNotExist"


_TAKES_VALUES: Final = frozenset({SelectorOperator.IN, SelectorOperator.NOT_IN})


@dataclass(frozen=True)
class SelectorRequirement:
    """One requirement on one Tag key. ``NotIn`` holds when the key is absent."""

    key: str
    operator: SelectorOperator
    values: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        # Kubernetes' own validation: an empty `In` would match nothing and an empty
        # `NotIn` everything, both silently; values on `Exists` would read as a filter
        # they are not.
        if (self.operator in _TAKES_VALUES) != bool(self.values):
            raise ValueError(
                f"{self.operator} on {self.key!r} "
                + ("needs values" if self.operator in _TAKES_VALUES else "takes no values")
            )

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "SelectorRequirement":
        return cls(
            key=str(payload["key"]),
            operator=SelectorOperator(payload["operator"]),
            values=tuple(str(value) for value in payload.get("values") or ()),
        )

    def as_payload(self) -> dict[str, Any]:
        return {"key": self.key, "operator": self.operator.value, "values": list(self.values)}

    def satisfied_by(self, tags: Mapping[str, str]) -> bool:
        match self.operator:
            case SelectorOperator.IN:
                return self.key in tags and tags[self.key] in self.values
            case SelectorOperator.NOT_IN:
                return self.key not in tags or tags[self.key] not in self.values
            case SelectorOperator.EXISTS:
                return self.key in tags
            case SelectorOperator.DOES_NOT_EXIST:
                return self.key not in tags


def selector_satisfied(selector: Iterable[SelectorRequirement], tags: Mapping[str, str]) -> bool:
    """Whether ``tags`` satisfies every requirement of ``selector`` (25 §9: all AND-ed)."""

    return all(requirement.satisfied_by(tags) for requirement in selector)


def selector_from_legacy(legacy: Mapping[str, str]) -> tuple[SelectorRequirement, ...]:
    """A legacy ``{key: value}`` selector as requirements that match exactly what it did."""

    return tuple(
        SelectorRequirement(key=key, operator=SelectorOperator.EXISTS)
        if value == PRESENT
        else SelectorRequirement(key=key, operator=SelectorOperator.IN, values=(value,))
        for key, value in legacy.items()
    )


class SelectorConflictError(ValueError):
    """The layers of a selector together demand something no Tag set can satisfy.

    Raised rather than resolved: silently dropping a layer would route work to a Runner
    the Contract narrowed *away* from.
    """


def narrow_selector(*layers: Iterable[SelectorRequirement]) -> tuple[SelectorRequirement, ...]:
    """Combine the selector layers: Product, Contract, Agent Runtime Profile (25 §9).

    Layers only narrow, so combining them is concatenation -- a requirement any layer
    states must hold. A combination that is provably empty on some key (``In [a]`` and
    ``In [b]``) is a configuration contradiction, not a narrowing
    (:class:`SelectorConflictError`).
    """

    combined = tuple(requirement for layer in layers for requirement in layer)
    for key in {requirement.key for requirement in combined}:
        _refuse_empty(key, [requirement for requirement in combined if requirement.key == key])
    return combined


def _refuse_empty(key: str, requirements: list[SelectorRequirement]) -> None:
    # A key is either absent or holds one value. The conjunction is empty when absence is
    # ruled out and so is every value.
    operators = {requirement.operator for requirement in requirements}
    absent_allowed = not operators & {SelectorOperator.IN, SelectorOperator.EXISTS}
    present_allowed = SelectorOperator.DOES_NOT_EXIST not in operators
    if present_allowed and SelectorOperator.IN in operators:
        allowed = set.intersection(
            *(set(r.values) for r in requirements if r.operator is SelectorOperator.IN)
        )
        allowed -= {
            value
            for requirement in requirements
            if requirement.operator is SelectorOperator.NOT_IN
            for value in requirement.values
        }
        present_allowed = bool(allowed)
    if not (absent_allowed or present_allowed):
        raise SelectorConflictError(
            f"the requirements on tag {key!r} exclude every value and its absence; "
            "no Runner can satisfy them"
        )


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
    # The legacy ``{key: value}`` selector. Kept beside ``requirements`` rather than
    # replaced so a Runner one minor behind, which reads only this field, still decodes
    # the payload during its `warn` window (ADR-0013 §7). Remove at the next major.
    selector: dict[str, str] = field(default_factory=dict)
    # The Contract the control plane admitted this Runner for (17 A2), and on a
    # wipe-and-reassign the departing one the wipe removes. `assert_routed` does not read
    # it: the single-Contract lock is held control-plane-side by `current_contract_id`.
    contract_id: str = ""
    # ADR-0022 §22: the selector as requirements. It and the legacy map must both hold.
    requirements: tuple[SelectorRequirement, ...] = ()
    # Spec: Runners rev 3: the host's LLM access the control plane chose for this
    # Directive. The Runner uses exactly this and fails closed when it no longer holds it.
    # None from a control plane below 3.24, whose Directive keeps the Runner's own choice.
    # It replaces `funding_party`, which a Runner on 3.24 drops from an older payload.
    llm_access: LlmAccess | None = None

    def selector_requirements(self) -> tuple[SelectorRequirement, ...]:
        """Everything this Directive was routed by: the legacy map and the requirements."""

        return (*selector_from_legacy(self.selector), *self.requirements)


@dataclass(frozen=True)
class RunnerRoutingIdentity:
    """What a Runner process knows about itself, as the control plane registered it.

    Built at the composition root from the bootstrap response and the config-declared
    Tags, and rebuilt with the Tags each heartbeat ack carries (ADR-0022 §13) -- never
    from an activity payload, which is the thing being checked.
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
    selector = routing.selector_requirements()
    if not selector_satisfied(selector, identity.tags):
        raise RoutingRefusedError(
            "selector_unsatisfied",
            f"this Runner's Tags do not satisfy the selector it was routed by "
            f"({sorted({requirement.key for requirement in selector})})",
        )
