"""The Grant snapshot the control plane hands a Runner for one Directive (ADR-0011 §11).

Activity I/O: a Runner with no heartbeat stream still takes this snapshot on the
activity payload (issue 36 retired the process cache), so it lives beside the other
activity contracts rather than in the Runner's own package. A registered Runner is
**pushed** it instead (issue 44) and holds it per Agent in
``agentic_runner.heartbeat_link``. The decision
function that reads it — ``decide_verb`` — is the Runner's either way, and stays there.

A Protected Path (ADR-0011 §13): a PR touching this module always hits a human gate, so
an Agent can never loosen the seams that bound it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Any, Final

from agentic_runner_contracts.grants.model import EMPTY_GRANT, Grant

REPO_RESOURCE_TYPE: Final = "repo"
# PRD issue 51: a Channel is registered as a Product resource so root grants and
# selectors reach it exactly as they reach a repository.
CHANNEL_RESOURCE_TYPE: Final = "channel"

CHANNEL_READ_VERB: Final = "read"
CHANNEL_WRITE_VERB: Final = "write"
PUSH_VERB: Final = "push"
PR_OPEN_VERB: Final = "pr.open"
PR_REVIEW_VERB: Final = "pr.review"
PR_MERGE_VERB: Final = "pr.merge"
PR_COMMENT_VERB: Final = "pr.comment"

# The org's four-eyes rule when the snapshot carries no Product rule for the repository —
# a Work Record with no Agent bound, or a backend that predates the field. One preserves
# the pre-platform behaviour: the single reviewer approval the Reviewer Gate already waits
# for (ADR-0011 s6).
DEFAULT_REQUIRED_HUMAN_APPROVALS: Final = 1


@dataclass(frozen=True)
class ResourceRegistration:
    """One row of the Contract-scoped resource registry carried by the snapshot.

    ``required_human_approvals`` is the owning Product's four-eyes rule, carried here
    because the Runner counts it at the merge seam and a repository belongs to exactly
    one Product (ADR-0011 s4, s6).
    """

    resource_type: str
    selector: str
    in_contract_scope: bool
    required_human_approvals: int = DEFAULT_REQUIRED_HUMAN_APPROVALS
    # PRD issue 52: the platform id behind the selector, where one exists -- a Channel's
    # id, which is what an envelope and a wake signal name it by (map ticket 10). A
    # repository has none: its selector is its name.
    resource_id: str | None = None
    # ADR-0018 §5: the Product that owns the resource, which is the Product an
    # Organisation-scoped Work Record binds to on its first ``repo.branch``. ``None`` only
    # from a backend that predates the field.
    product_id: str | None = None


@dataclass(frozen=True)
class ProductReach:
    """One Product an Organisation-scoped Work Record can reach (ADR-0018 §3, §6).

    Its repositories are the registry rows carrying this ``product_id``. The Runner
    selector is the Product's own default, which the Runner checks its tags against
    before any verb on one of those repositories.
    """

    product_id: str
    runner_selector: Mapping[str, str]


@dataclass(frozen=True)
class GrantSnapshot:
    """The three Grants plus the registry, as the control plane hands them over.

    ``enforced`` is always true from the platform (issue 88): a Work Record with no Agent
    bound is held, never run unattenuated. A false one decides ``allow`` and says so in
    its Evidence; it survives only for wire compatibility with installed Runners.
    """

    agent_id: str | None = None
    contract_id: str | None = None
    contract_state: str = "active"
    dispatchable: bool = True
    # PRD issue 31: "ok" | "empty" | "invalid" (22 A6). Defaults to "ok" so a payload from
    # a backend that predates this field -- every existing fixture and test -- reads
    # exactly as before.
    llm_credential_status: str = "ok"
    # Local-agents 03: the reason an "invalid" slot fails closed on its mode (a
    # subscription off the person's own Runner, a setup token), or "" -- also for a
    # payload from a backend that predates the field.
    llm_credential_reason: str = ""
    root: Grant = EMPTY_GRANT
    agent: Grant = EMPTY_GRANT
    persona: Grant = EMPTY_GRANT
    # PRD issue 34, map ticket 21 A5: the Account Contract root a Leaf's Effective Grant
    # also intersects. ``None`` -- every direct Contract, and every payload from a backend
    # that predates the field -- leaves the chain three links long, exactly as before.
    account_contract: Grant | None = None
    account_contract_id: str | None = None
    resources: tuple[ResourceRegistration, ...] = ()
    # ADR-0018 §3: the live Products of the Contract's effective scope. Re-read with the
    # rest of the snapshot, so a Product dropped from the Contract leaves at the next
    # Directive. Empty from a backend that predates the field.
    reach: tuple[ProductReach, ...] = ()
    # TODO(issue 88): remove ``enforced`` at the next contracts major.
    enforced: bool = True

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> GrantSnapshot:
        """Parse ``GET /api/internal/agents/{id}/grant-snapshot`` (services/agents.py)."""

        return cls(
            agent_id=str(payload["agent_id"]),
            contract_id=str(payload["contract_id"]),
            contract_state=str(payload["contract_state"]),
            dispatchable=bool(payload["dispatchable"]),
            llm_credential_status=str(payload.get("llm_credential_status", "ok")),
            llm_credential_reason=str(payload.get("llm_credential_reason", "")),
            root=Grant.model_validate(payload["root_grant"]),
            agent=Grant.model_validate(payload["agent_grant"]),
            persona=Grant.model_validate(payload["persona_allow_list"]),
            account_contract=(
                None
                if payload.get("account_contract_root_grant") is None
                else Grant.model_validate(payload["account_contract_root_grant"])
            ),
            account_contract_id=(
                None
                if payload.get("account_contract_id") is None
                else str(payload["account_contract_id"])
            ),
            resources=tuple(
                ResourceRegistration(
                    resource_type=str(resource["resource_type"]),
                    selector=str(resource["selector"]),
                    in_contract_scope=bool(resource["in_contract_scope"]),
                    required_human_approvals=int(
                        resource.get("required_human_approvals", DEFAULT_REQUIRED_HUMAN_APPROVALS)
                    ),
                    resource_id=(
                        None
                        if resource.get("resource_id") is None
                        else str(resource["resource_id"])
                    ),
                    product_id=(
                        None if resource.get("product_id") is None else str(resource["product_id"])
                    ),
                )
                for resource in payload.get("resources", ())
            ),
            reach=tuple(
                ProductReach(
                    product_id=str(product["product_id"]),
                    runner_selector={
                        str(key): str(value)
                        for key, value in (product.get("runner_selector") or {}).items()
                    },
                )
                for product in payload.get("reach", ())
            ),
        )

    def product_of(self, *, identifier: str, resource_type: str = REPO_RESOURCE_TYPE) -> str | None:
        """The in-scope Product that owns this resource, or ``None`` if none reaches it.

        A repository belongs to exactly one Product (``product_resources.selector`` is
        unique), so the first match is the only one.
        """

        return next(
            (
                registration.product_id
                for registration in self.resources
                if registration.resource_type == resource_type
                and registration.in_contract_scope
                and fnmatchcase(identifier, registration.selector)
            ),
            None,
        )

    def runner_selector_of(self, product_id: str) -> Mapping[str, str] | None:
        """The Product's Runner selector, or ``None`` if the Product is out of reach."""

        return next(
            (product.runner_selector for product in self.reach if product.product_id == product_id),
            None,
        )

    def channel_selector(self, channel_id: str) -> str | None:
        """The registry selector a Channel id evaluates as, or ``None`` if unregistered.

        The seam evaluates ``(channel, <selector>, read|write)`` because that is what a
        Grant entry globs over; the Agent names the Channel by id because that is what
        travels in an envelope. An id the registry does not carry is out of scope by
        construction (ADR-0011 §4) and the caller evaluates the id itself, which denies.
        """

        return next(
            (
                registration.selector
                for registration in self.resources
                if registration.resource_type == CHANNEL_RESOURCE_TYPE
                and registration.resource_id == channel_id
            ),
            None,
        )

    def in_contract_scope(self, *, resource_type: str, identifier: str) -> bool:
        """Whether the registry places this resource inside the Contract's Product scope.

        An unregistered resource is out of scope by construction (ADR-0011 §4): the
        registry is the only thing that binds a name to a Product, so a name it does not
        carry is unreachable whatever the selector says.
        """

        return any(
            registration.resource_type == resource_type
            and registration.in_contract_scope
            and fnmatchcase(identifier, registration.selector)
            for registration in self.resources
        )

    def required_human_approvals(
        self, *, identifier: str, resource_type: str = REPO_RESOURCE_TYPE
    ) -> int:
        """The Product rule the merge seam counts against for this resource.

        The strictest matching registration wins, the same reading the grant evaluator
        takes: several selectors may glob one repository, and only "most restrictive"
        can ever ask for too many approvals rather than too few.
        """

        matches = [
            registration.required_human_approvals
            for registration in self.resources
            if registration.resource_type == resource_type
            and registration.in_contract_scope
            and fnmatchcase(identifier, registration.selector)
        ]
        return max(matches, default=DEFAULT_REQUIRED_HUMAN_APPROVALS)


UNENFORCED_SNAPSHOT: Final = GrantSnapshot(enforced=False)
