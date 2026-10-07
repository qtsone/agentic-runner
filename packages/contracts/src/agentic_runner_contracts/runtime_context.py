"""The control-plane payload a Runner activity parses for one Work Record.

Served by ``/api/runner/v1/work-records/{id}/runtime-context`` and validated here, so the
platform that writes it and the Runner that reads it agree on one model (ADR-0013 §4, §7).
:class:`WorkerRuntimeContextResolver` at the foot of this module is the fetch, shared for
the same reason: both sides resolve this context, through clients of their own.
"""

from __future__ import annotations

import re
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

_FORBIDDEN_SECRET_VALUE_FIELDS = frozenset(
    {
        "secret_value",
        "token_value",
        "api_key_value",
        "private_key_value",
        "password_value",
    }
)


class ProductVerifierCommandSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str
    available: bool
    metadata: dict[str, object] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def reject_secret_value_fields(cls, data: object) -> object:
        _reject_forbidden_secret_value_fields(data)
        return data


# The Agent Runtime Profile's ``network_policy`` key naming the destinations a Directive
# may reach (PRD issue 58, 06 §4): host names, ``host:port`` or ``*.domain`` patterns.
# The Runner adds the ones it knows the Directive needs -- the git remote and each
# granted Streamable HTTP MCP server -- and the LLM proxy is on loopback.
EGRESS_ALLOW_LIST_KEY = "egress"


class McpServerSpec(BaseModel):
    """One registered MCP server bound to the Work Record's Product (PRD issue 58).

    What the Runner needs to write the server into a CLI's native config -- never a
    credential value: ``credential_reference`` is a *name* the Runner resolves out of its
    own host store, and a server that names one is started by the Runner, not the CLI
    (17 A4). ``tools`` are the verbs an ``mcp:<slug>`` Grant entry decides over.
    """

    model_config = ConfigDict(extra="forbid")

    slug: str
    transport: Literal["stdio", "streamable_http"]
    config: dict[str, object] = Field(default_factory=dict)
    credential_reference: str | None = None
    required: bool = False
    tools: list[str] = Field(default_factory=list)


class WorkerRuntimeContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    work_record_id: str
    profile_slug: str
    cli_kind: Literal["codex_cli", "claude_code"]
    repo: str
    base_branch: str
    reviewer: str | None = None
    # The Agent this Work Record's Directives are attributed to (ADR-0011 §10). None for a
    # Work Record created before the Agent entity (issue 07) or by a flow that binds none;
    # the Runner then enforces an unattenuated snapshot rather than refusing every verb.
    agent_id: str | None = None
    # The Contract the Agent works under, for Usage Record attribution (map ticket 12 B2).
    # None wherever `agent_id` is.
    contract_id: str | None = None
    # PRD issue 31: what the Agent is configured to run, for pricing a harness usage
    # event that carries no model of its own (Codex `turn.completed.usage`). Defaulted
    # for the same version-skew reason as the fields above.
    model: str | None = None
    # The Work Record's Budget, written by console intake (PRD issue 17) and left null by
    # Slack intake. Null per dimension means the worker's own env default applies.
    budget_max_directives: int | None = None
    budget_max_wall_clock_seconds: float | None = None
    budget_max_tokens: int | None = None
    # The Contract's `runner_host` (PRD issue 47): a `user`-hosted Contract runs on a
    # laptop whose sleep is an outage the wall-clock keeps counting through, so its
    # default wall-clock Budget is the longer one. None where no Contract is bound.
    runner_host: str | None = None
    # The Work Record's task text (Slack markup already stripped by the backend).
    # Defaulted so a worker on this version tolerates a backend that predates it.
    completion_criteria: str = ""
    # The routed specialist Persona and its operator-authored instructions, prepended to
    # Directive prompts (persona-specialists issue 02). Defaulted for the same version
    # skew; empty instructions leave the prompts byte-identical to the pre-Persona form.
    persona_slug: str | None = None
    persona_instructions: str = ""
    # PRD issue 56, map 09: the accepted Lessons this Directive carries, already bounded
    # and rendered by the control plane (a truncation note included), and their ids for
    # the ``experience_injected`` Evidence. Empty text leaves every prompt byte-identical
    # to one without Experience; defaulted for the same version skew as above.
    experience: str = ""
    experience_lesson_ids: list[str] = Field(default_factory=list)
    experience_truncated: bool = False
    task_queue: str
    worker_secret_refs: list[str]
    # PRD issue 48, map 22 A1 / 25 §11: the Credential Reference *names* on the
    # Contract's manifest. Names bound what this Directive may resolve out of the host
    # store; the values themselves live only on whichever side hosts the Runner and never
    # cross this payload (ADR-0013 §11). Defaulted for the same version-skew reason as
    # the fields above -- an older backend serves no manifest and the Runner resolves
    # nothing, which fails closed.
    credential_references: list[str] = Field(default_factory=list)
    command_policy: dict[str, object] = Field(default_factory=dict)
    network_policy: dict[str, object] = Field(default_factory=dict)
    # PRD issue 58: the registry rows bound to this Work Record's Product. The Runner
    # decides per Directive which of them the Agent's Effective Grant lets it write into
    # the CLI's config; defaulted for the same version skew -- an older backend binds none.
    mcp_servers: list[McpServerSpec] = Field(default_factory=list)
    product_verifier_command_source: ProductVerifierCommandSource
    # The Autonomy Policy's tier source (PRD issue 14): the Work Record's own Action/Risk
    # tier (null for a pre-Release-E record -- the activity holds for human on that alone),
    # the owning Product's Data Class, its id and the requester's, and whether the pair
    # sits at or under the Contract's derived Mandate ceiling. Defaulted for the same
    # version-skew reason as the fields above; `tier_within_contract_ceiling` defaults to
    # the safe reading (never within ceiling) rather than a silent allow.
    product_id: str | None = None
    data_class: str | None = None
    action_tier: str | None = None
    risk_tier: str | None = None
    requester_id: str | None = None
    tier_within_contract_ceiling: bool = False
    # PRD issue 59: `work` or `epic` (or `report`, console-v2 issue 23), the parent a child
    # reports to, the sibling a `based_on` child stacks on (``base_branch`` is then that
    # sibling's branch) and the Epic's coordination Budget. Defaulted for the same version
    # skew as above.
    kind: Literal["work", "epic", "report"] = "work"
    parent_work_record_id: str | None = None
    stack_base_work_record_id: str | None = None
    coordination_budget_max_directives: int | None = None
    coordination_budget_max_tokens: int | None = None

    @model_validator(mode="before")
    @classmethod
    def reject_secret_value_fields(cls, data: object) -> object:
        _reject_forbidden_secret_value_fields(data)
        return data


def _reject_forbidden_secret_value_fields(value: object) -> None:
    if isinstance(value, dict):
        forbidden_fields = _FORBIDDEN_SECRET_VALUE_FIELDS.intersection(value)
        if forbidden_fields:
            raise ValueError(
                "Runtime context must not include secret value fields: "
                + ", ".join(sorted(forbidden_fields))
            )
        for nested_value in value.values():
            _reject_forbidden_secret_value_fields(nested_value)
    elif isinstance(value, list):
        for nested_value in value:
            _reject_forbidden_secret_value_fields(nested_value)


_BRANCH_SLUG_MAX_LENGTH = 48
_SLUG_TOKEN_RE = re.compile(r"[^a-z0-9]+")


def work_branch_name(*, work_record_id: str, profile_slug: str) -> str:
    """The branch a Work Record's Directives push to.

    A contract, not a convenience: the platform writes this name into the PR body and the
    branch it opens the PR from, and the Runner checks out and pushes the same name. One
    definition, so the two sides cannot drift into opening a PR on a branch nothing pushed.
    """

    return f"agent/{work_record_id}-{sanitize_slug(profile_slug)}"


def sanitize_slug(value: str) -> str:
    """A safe, bounded lower-case token: the one spelling both sides slug names with."""

    slug = _SLUG_TOKEN_RE.sub("-", value.strip().lower()).strip("-")
    if not slug:
        return "work"
    return slug[:_BRANCH_SLUG_MAX_LENGTH].strip("-") or "work"


class RuntimeContextFastApiClient(Protocol):
    """The one call the resolver needs, so either side's client satisfies it."""

    async def get_runtime_context(
        self, work_record_id: str, agent_id: str | None = None
    ) -> dict[str, Any]: ...


class WorkerRuntimeContextResolver:
    """Fetch and validate one Work Record's runtime context.

    A contract rather than either side's own: the Runner resolves it to run a Directive
    and the platform's `assemble_context` resolves it to build one, and a resolver that
    validated differently on the two sides would be a drift nothing else would catch.
    """

    def __init__(self, fastapi_client: RuntimeContextFastApiClient) -> None:
        self._fastapi_client = fastapi_client

    async def resolve(
        self, work_record_id: str, *, agent_id: str | None = None
    ) -> WorkerRuntimeContext:
        """The Work Record's context -- or, named, one Swarm member's own (PRD issue 53).

        The member is passed positionally only when named, so a client that predates
        the parameter still serves the degenerate, one-Agent case.
        """

        payload = (
            await self._fastapi_client.get_runtime_context(work_record_id, agent_id)
            if agent_id
            else await self._fastapi_client.get_runtime_context(work_record_id)
        )
        return WorkerRuntimeContext.model_validate(payload)
