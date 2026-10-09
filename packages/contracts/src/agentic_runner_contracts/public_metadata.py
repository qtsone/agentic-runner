"""Public Metadata builders: every Temporal-visible name is generated here (ADR-0010 §6).

Public Metadata is everything the workflow engine exposes that payload encryption never
covers — workflow and activity type names, workflow ids, task-queue and namespace names,
Search Attributes and memo. The invariant (map ticket 07 §1–§3): a name may say what the
*platform does*, never *whose work it is*. Only platform-issued ids and platform enums
enter one; an organisation name, product slug, repository, branch, email or any
operator-typed free text never does.

This module is the single place those names are built. The naming gate
(``tests/integration/test_public_metadata_naming_gate.py``) fails the build when a string
literal reaches ``task_queue=``, ``namespace=``, ``search_attributes=``, ``memo=``,
``workflow_id=`` or Temporal's own ``id=`` anywhere else under ``src/``.

Kept pure and stdlib-only on purpose: ``workflows/`` is re-imported inside Temporal's
determinism sandbox on worker startup, and migration 0029 imports these builders too, so
the derived task-queue name has exactly one definition. The platform enums are mirrored
here as value tuples rather than imported from ``models.domain`` — importing SQLAlchemy
into a sandboxed module is what the determinism gate exists to prevent;
``tests/unit/test_public_metadata.py`` pins each tuple to its ``StrEnum``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from enum import StrEnum
from typing import Final
from uuid import UUID

NAMESPACE_PREFIX = "org-"
# The one namespace that is nobody's Organisation (PRD issue 64): platform-wide sweeps run
# here once, the way Kubernetes runs cluster-wide controllers in `kube-system` rather than
# a copy per tenant namespace. Deliberately outside the `org-` prefix, so `is_namespace`
# can never mistake it for one.
CONTROL_PLANE_NAMESPACE = "control-plane"
_SEPARATOR = "."
# `agent_runtime_profiles.temporal_task_queue` is String(128) (models/domain.py) and
# `TemporalTaskQueueText` caps at the same number.
TASK_QUEUE_MAX_LENGTH = 128
# The two platform-served queues, one pair per Organisation namespace (ADR-0013 §1,
# map ticket 25 §2). Fixed names, deliberately: the namespace disambiguates, so nothing
# about an Organisation may enter them. `runner.{runner_id}` is the third, per Runner.
LOOP_TASK_QUEUE = "loop"
CONTROL_TASK_QUEUE = "control"
RUNNER_TASK_QUEUE_PREFIX = "runner."

# Mirrors of models.domain enums — pinned by tests/unit/test_public_metadata.py.
OUTCOME_KINDS = ("pull_request", "routing_decision", "report")
RUNTIME_KINDS = ("codex_cli", "claude_code")
ACTION_TIERS = ("observe", "draft", "change", "critical")
RISK_TIERS = ("low", "medium", "high")
DATA_CLASSES = ("public", "internal", "confidential", "restricted")
# Declaration order, which is also the order a Runtime Profile's Personas are ranked in
# (see `primary_persona`): the Specialisation a fleet exists for comes first.
SPECIALISATIONS = ("development", "triage", "review", "ops")
PERSONA_CATALOGUES = ("platform", "organisation", "account")
PLATFORM_CATALOGUE = "platform"

# The closed, review-gated Search Attribute catalogue (map ticket 07 §5). `organisation_id`
# is deliberately *not* registered: the namespace is the Organisation, so no query ever
# spans one, and SQL visibility allows only ten custom Keyword attributes per namespace —
# dropping it leaves a spare slot. It stays in the catalogue as a memo field.
MEMO_ONLY_ATTRIBUTES = frozenset({"organisation_id"})
REGISTERED_SEARCH_ATTRIBUTES = (
    "product_id",
    "contract_id",
    "agent_id",
    "work_record_id",
    "persona",
    "outcome_kind",
    "action_tier",
    "risk_tier",
    "data_class",
)
SEARCH_ATTRIBUTE_CATALOGUE = tuple(sorted({*REGISTERED_SEARCH_ATTRIBUTES, *MEMO_ONLY_ATTRIBUTES}))

# The persona component is checked for *shape* (a slug: lowercase, digits, hyphens) rather
# than membership, because a Platform Operator creates the platform catalogue's rows. Shape
# alone is no defence against an Organisation's or a user's own slug, which is why only
# `persona_name` may produce the component (ADR-0019 §4). The pattern is
# character-for-character the one the Persona/Product/Profile admin schemas validate a slug
# with: anything stricter here rejects a Persona the API already accepted,
# which surfaces as a 500 on the profile write and aborts migration 0029 mid-upgrade.
_SLUG = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,126}[a-z0-9])?$")
# Platform verbs, dotted (`pr.review`), the form map ticket 25 fixed for hook names.
_VERB = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$")


class PublicMetadataError(ValueError):
    """Raised when a value offered for a Temporal-visible name is not platform vocabulary."""


def namespace(organisation_id: UUID) -> str:
    """The Temporal namespace that isolates one Organisation (map ticket 07 §4)."""
    return f"{NAMESPACE_PREFIX}{organisation_id}"


def control_plane_namespace() -> str:
    """The namespace the platform's global Schedules live in (PRD issue 64).

    ``plan.reconcile`` and ``namespace.reconcile`` sweep every Account and every pending
    Organisation, so a copy per Organisation namespace ran each sweep N times -- harmless
    only while every step stays idempotent.
    """
    return CONTROL_PLANE_NAMESPACE


def is_namespace(value: str) -> bool:
    """Whether ``value`` is a namespace this module would build.

    Used by ``AppSettings`` to fail a deployed environment fast on a hand-typed namespace:
    the Release D cut (PRD issue 16) is one GitOps commit, and a typo there routes every
    workflow into a namespace nobody polls.
    """
    if not value.startswith(NAMESPACE_PREFIX):
        return False
    try:
        organisation_id = UUID(value[len(NAMESPACE_PREFIX) :])
    except ValueError:
        return False
    return namespace(organisation_id) == value


def task_queue_loop() -> str:
    """The queue a namespace's workflow tasks are served on (ADR-0013 §1, map ticket 25 §2).

    Fixed, not derived: the namespace already disambiguates one Organisation from every
    other, so the queue only has to say which half of the Platform Worker serves it.
    """
    return LOOP_TASK_QUEUE


def task_queue_control() -> str:
    """The queue a namespace's *platform* activities are served on (ADR-0013 §1).

    The other half of the Platform Worker pair. An activity lands here iff the locality
    rule (``workflows/activity_registry.py``) says it is the control plane's own
    bookkeeping rather than work that must happen where the workspace is.
    """
    return CONTROL_TASK_QUEUE


def task_queue_runner(runner_id: UUID) -> str:
    """The queue one Runner serves its activities on (ADR-0013 §2).

    The queue *is* the Runner identity, which is what made map ticket 15's open question
    ("which queues may a Runner identity poll") a tautology. ``runner_id`` is the
    platform-issued Runner id, never an operator-typed fleet name: a fleet id is exactly
    the free text §6 bans from a displayed name (see :func:`ops_probe_id`).
    """
    return f"{RUNNER_TASK_QUEUE_PREFIX}{runner_id}"


def runner_id_of_task_queue(task_queue: str) -> UUID | None:
    """The Runner a queue belongs to, or ``None`` when it is not a Runner's queue.

    The inverse of :func:`task_queue_runner`, round-tripped through it so the only
    accepted spelling is the one the builder makes (e.g. an upper-case UUID is not one).
    """
    candidate = task_queue.removeprefix(RUNNER_TASK_QUEUE_PREFIX)
    try:
        runner_id = UUID(candidate)
    except ValueError:
        return None
    return runner_id if task_queue_runner(runner_id) == task_queue else None


def task_queue(*, persona: str | None, runtime_kind: str, profile_id: UUID) -> str:
    """The Agent Runtime Profile's routing key (map ticket 07 §4, amended by 25 §2).

    **No longer a polled queue.** Since PRD issue 37 the only queues anything polls are
    :func:`task_queue_loop`, :func:`task_queue_control` and :func:`task_queue_runner`;
    this name survives as the Runner-side routing key issue 42 matches a Directive on,
    and as ``agent_runtime_profiles.temporal_task_queue``. Nothing passes it as
    ``task_queue=``.

    ``persona`` is the Profile's primary bound Persona (see `primary_persona`), as
    :func:`persona_name` spells it. It is
    optional because the binding is a soft, many-to-one reference the Profile does not
    own: a Profile no Persona names yet is addressed by its runtime kind and id alone,
    which is still nothing but platform vocabulary.
    """
    _require_enum("runtime_kind", runtime_kind, RUNTIME_KINDS)
    addressable = f"{runtime_kind}{_SEPARATOR}{profile_id}"
    if persona is None:
        return addressable
    # The Persona component is decoration — `profile_id` is what makes the queue unique —
    # so an over-long slug is clipped rather than rejected. A Persona slug may be 128
    # characters (services/personas.py), which would mint a ~177-character queue and
    # overflow the String(128) column: a DataError the profile write does not catch (500)
    # and, in migration 0029, an `alembic upgrade head` that aborts mid-sync.
    budget = TASK_QUEUE_MAX_LENGTH - len(addressable) - len(_SEPARATOR)
    persona = _require_slug("persona", persona)[:budget].rstrip("-")
    return f"{persona}{_SEPARATOR}{addressable}"


def runner_task_queue(runner_id: UUID) -> str:
    """The queue one registered Runner polls (map ticket 06 §4 as amended by 25 §12).

    A Runner is activity-only: the loop runs on platform-hosted Platform Workers and
    dispatches each Directive to exactly one Runner by name, so the queue is the Runner's
    own platform-issued id and nothing else. Runner Tags, the host party and the
    Organisation deliberately stay out of it -- tags are operator-typed free text (§6
    bans that from a displayed name), and host party is a routing rule the control plane
    resolves before it picks the queue (17 A3), never a name Temporal displays.
    """
    return f"runner{_SEPARATOR}{runner_id}"


def persona_name(*, slug: str, catalogue: str, persona_id: UUID) -> str:
    """The component one Persona contributes to any Temporal-visible name (ADR-0019 §4).

    A platform Persona contributes its slug, byte-for-byte the string every name built
    before catalogues carries. Any other Persona contributes its platform-issued id, never
    the slug its author chose: that slug is unique only within its catalogue, so it cannot
    be unique in a queue name, and it is an Organisation's or a user's own free text --
    the channel out of the Organisation this module exists to close.
    """
    _require_enum("catalogue", catalogue, PERSONA_CATALOGUES)
    if catalogue == PLATFORM_CATALOGUE:
        return _require_slug("persona", slug)
    return str(persona_id)


def primary_persona(personas: Iterable[tuple[str, str]]) -> str | None:
    """Pick the one Persona name a Runtime Profile's queue name carries.

    Several Personas may bind to one Profile (QTS runs `dev-engineer` and `code-reviewer`
    on the same fleet) while the Profile stores a single queue, so the pick has to be
    deterministic and the same in the service and in migration 0029. Rank is the platform's
    own Specialisation declaration order — the Specialisation a fleet exists for wins —
    then the name. ``personas`` is (name, specialisation) pairs, each name from
    :func:`persona_name`; empty yields ``None``.
    """
    ranked = sorted(
        personas,
        key=lambda persona: (_specialisation_rank(persona[1]), persona[0]),
    )
    return ranked[0][0] if ranked else None


def workflow_id(*, outcome_kind: str, work_record_id: UUID) -> str:
    """The id of the workflow that drives one Work Record to its Outcome."""
    _require_enum("outcome_kind", outcome_kind, OUTCOME_KINDS)
    return f"{outcome_kind}{_SEPARATOR}{work_record_id}"


def seat_sync_workflow_id(*, event: str, contract_id: UUID) -> str:
    """The id of the workflow that prorates one Contract's seat delta (PRD issue 25).

    ``event`` is the platform verb the Contract reached (``contract.accepted`` /
    ``contract.terminated``): each fires at most once per Contract, so pairing it with
    the Contract id is enough uniqueness with no nonce.
    """
    return f"{_require_verb('event', event)}{_SEPARATOR}{contract_id}"


def system_job_id(job: str) -> str:
    """The id of a platform-wide scheduled job's Schedule and target workflow.

    Distinct from :func:`ops_probe_id` (one nonce per manual, operator-triggered probe):
    a Schedule reuses one stable id across every run, which is what makes ensuring it at
    worker startup idempotent (create, or re-point the one already there) rather than
    minting a new Schedule on every restart.
    """
    return _require_verb("job", job)


def routine_schedule_id(*, routine_id: UUID) -> str:
    """The id of one Routine's Schedule and of the workflow each of its firings starts
    (console-v2 issue 15, ADR-0017 §2).

    Stable per Routine, as :func:`system_job_id` is per job, so ensuring the Schedule on
    every edit re-points the one already there. Temporal suffixes the scheduled time to
    the workflow id of each firing, which keeps two firings of one Routine distinct. The
    Routine's title and instructions are operator-typed free text and never enter it.
    """

    return f"routine{_SEPARATOR}{routine_id}"


def triage_workflow_id(*, nonce: UUID) -> str:
    """The id of a Triage Directive workflow, which drives no Work Record yet.

    One admitted message is one run; a platform-issued nonce is all the uniqueness there
    is to key on before any Work Record exists (mirrors :func:`ops_probe_id`).
    """

    return f"triage.directive{_SEPARATOR}{nonce}"


def ops_probe_id(*, probe: str, nonce: UUID) -> str:
    """The id of an operator-triggered probe workflow, which drives no Work Record.

    A Codex fleet re-check answers to whoever pressed the button, so `workflow_id` — keyed
    on a Work Record — has no shape for it. The probe being run is a platform verb and the
    nonce is platform-issued. The fleet is deliberately *not* in the name: a fleet id is
    operator-typed (`CODEX_FLEETS[].fleet_id`) and is exactly the free text §6 bans from a
    displayed name; it travels in the workflow's encrypted arguments instead.
    """
    return f"{_require_verb('probe', probe)}{_SEPARATOR}{nonce}"


def search_attributes(
    *,
    product_id: UUID | None = None,
    contract_id: UUID | None = None,
    agent_id: UUID | None = None,
    work_record_id: UUID | None = None,
    persona: str | None = None,
    outcome_kind: str | None = None,
    action_tier: str | None = None,
    risk_tier: str | None = None,
    data_class: str | None = None,
) -> dict[str, str]:
    """The registered Keyword attributes for one execution, omitting the unset ones.

    Search Attributes exist to be queried, so they are never encoded; the whole catalogue
    is therefore ids and enums. `organisation_id` is not here — it is `memo`. ``persona``
    is what :func:`persona_name` returns, never a raw slug.
    """
    if persona is not None:
        _require_slug("persona", persona)
    if outcome_kind is not None:
        _require_enum("outcome_kind", outcome_kind, OUTCOME_KINDS)
    if action_tier is not None:
        _require_enum("action_tier", action_tier, ACTION_TIERS)
    if risk_tier is not None:
        _require_enum("risk_tier", risk_tier, RISK_TIERS)
    if data_class is not None:
        _require_enum("data_class", data_class, DATA_CLASSES)
    values: dict[str, object | None] = {
        "product_id": product_id,
        "contract_id": contract_id,
        "agent_id": agent_id,
        "work_record_id": work_record_id,
        "persona": persona,
        "outcome_kind": outcome_kind,
        "action_tier": action_tier,
        "risk_tier": risk_tier,
        "data_class": data_class,
    }
    return {key: str(value) for key, value in values.items() if value is not None}


def memo(*, organisation_id: UUID) -> dict[str, str]:
    """The memo carried beside the Search Attributes: the Organisation that owns the run."""
    return {"organisation_id": str(organisation_id)}


def signal_name(verb: str) -> str:
    """The name a signal (or hook) is sent under: a platform verb, dotted.

    No catalogue yet — Release 1 has no signals — so this validates the vocabulary rather
    than enumerating it; the gates that add signals (PRD issues 11, 19) name them here.
    """
    return _require_verb("signal name", verb)


class HookName(StrEnum):
    """The closed Runner Hook catalogue (ADR-0013 §10, map ticket 26 §1).

    Operators fill slots; nobody adds one. The catalogue is Public Metadata for the same
    reason a queue name is: a hook name is displayed, logged and carried in Evidence, so
    it may say what the *platform does* and never whose work it is. A repository- or
    plugin-supplied hook has no name here because neither exists — repository hooks would
    let an Agent author code the Runner then executes outside the command policy, and MCP
    is the extension point instead of plugins (map ticket 05).

    Declaration order below ``RUNNER_SHUTDOWN`` is the **execution order**
    (:data:`DIRECTIVE_HOOK_ORDER`); the two Runner-lifecycle slots sit outside it.
    """

    RUNNER_STARTUP = "runner_startup"
    RUNNER_SHUTDOWN = "runner_shutdown"
    PRE_DIRECTIVE = "pre_directive"
    ENVIRONMENT = "environment"
    PRE_CHECKOUT = "pre_checkout"
    CHECKOUT = "checkout"
    POST_CHECKOUT = "post_checkout"
    PRE_RUNTIME = "pre_runtime"
    POST_RUNTIME = "post_runtime"
    PRE_VERIFY = "pre_verify"
    POST_VERIFY = "post_verify"
    PRE_ARTIFACT = "pre_artifact"
    POST_ARTIFACT = "post_artifact"
    PRE_EXIT = "pre_exit"


DIRECTIVE_HOOK_ORDER: Final[tuple[HookName, ...]] = (
    HookName.PRE_DIRECTIVE,
    HookName.ENVIRONMENT,
    HookName.PRE_CHECKOUT,
    HookName.CHECKOUT,
    HookName.POST_CHECKOUT,
    HookName.PRE_RUNTIME,
    HookName.POST_RUNTIME,
    HookName.PRE_VERIFY,
    HookName.POST_VERIFY,
    HookName.PRE_ARTIFACT,
    HookName.POST_ARTIFACT,
    HookName.PRE_EXIT,
)

# Everything up to and including `pre_runtime` is fatal on a non-zero exit: refusing after
# the Agent Runtime has already run would leave the Workspace half-changed with nothing to
# roll back to. `post_runtime` onwards is recorded and carried on — in particular
# `post_verify`, which map ticket 26 §1 fixes as never changing the Verifier's verdict.
FATAL_HOOKS: Final[frozenset[HookName]] = frozenset(
    DIRECTIVE_HOOK_ORDER[: DIRECTIVE_HOOK_ORDER.index(HookName.PRE_RUNTIME) + 1]
)


def hook_name(value: str) -> HookName:
    """The catalogue slot ``value`` names, or a refusal.

    The Runner calls this on every filename it finds in the hooks path, so a typo
    (``pre-directive``, ``post_command``) fails loudly at load instead of installing a
    hook that silently never runs.
    """

    try:
        return HookName(value)
    except ValueError:
        raise PublicMetadataError(
            f"hook name {value!r} is not in the catalogue; allowed: "
            f"{', '.join(name.value for name in HookName)}"
        ) from None


def _specialisation_rank(specialisation: str) -> int:
    return (
        SPECIALISATIONS.index(specialisation)
        if specialisation in SPECIALISATIONS
        else len(SPECIALISATIONS)
    )


def _require_enum(field: str, value: str, allowed: tuple[str, ...]) -> str:
    if value not in allowed:
        raise PublicMetadataError(
            f"{field} {value!r} is not a platform enum; allowed: {', '.join(allowed)}"
        )
    return value


def _require_verb(field: str, value: str) -> str:
    if _VERB.fullmatch(value) is None:
        raise PublicMetadataError(
            f"{field} {value!r} is not a platform verb (lowercase, dotted, e.g. 'pr.review')"
        )
    return value


def _require_slug(field: str, value: str) -> str:
    if _SLUG.fullmatch(value) is None:
        raise PublicMetadataError(f"{field} {value!r} is not a platform slug")
    return value
