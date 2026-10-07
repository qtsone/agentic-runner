"""The Verb Catalogue — platform-defined, closed and versioned (ADR-0011 §3).

A verb exists here only once the Runner has a seam that enforces it: a verb with no seam
behind it is a lie in the grant. ``pr.comment`` was therefore *reserved* in v1 and v2 — the
name taken so nothing else claimed it, refused in every Grant — until the Critic's seam
landed in v3 (M4, PRD issue 54). ``channel.read`` / ``channel.write`` were reserved the
same way until the Message store registered their seam (M4, PRD issue 52).

A reserved verb is still *catalogued*: it is listed by the console and answered about by
the evaluator, so the vocabulary is visible before the seam exists. Only
:meth:`VerbCatalogue.grantable` — what a Grant may carry, and what a wildcard expands
over — leaves it out.

A shipped version is frozen. A Grant pins the version it was written against and its verb
wildcards were expanded against that version, so a platform release that adds a verb never
silently widens a Contract nobody re-saved. Resource *selectors* are the opposite: they
resolve live, because resources are the org's vocabulary and change with the org.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


class UnknownCatalogueVersionError(LookupError):
    """Raised when a stored Grant pins a catalogue version this release does not ship."""


@dataclass(frozen=True)
class VerbCatalogue:
    """One frozen version of the catalogue: resource type -> the verbs it supports."""

    version: str
    verbs: dict[str, frozenset[str]]
    reserved: dict[str, frozenset[str]]
    # Whether ``mcp:<server>`` entries are live under this version: their wildcards
    # expand, at write time, over the server's registered tool names (PRD issue 58).
    # Under an older pin the entries stay exactly as stored until the Grant is re-saved.
    expands_mcp: bool = False

    def knows(self, resource_type: str) -> bool:
        return resource_type in self.verbs

    def verbs_for(self, resource_type: str) -> frozenset[str]:
        return self.verbs.get(resource_type, frozenset())

    def is_reserved(self, resource_type: str, verb: str) -> bool:
        return verb in self.reserved.get(resource_type, frozenset())

    def grantable(self, resource_type: str) -> frozenset[str]:
        """The verbs of ``resource_type`` a Grant may actually carry today.

        A reserved verb is catalogued — the console lists it and the evaluator answers
        about it — but no Grant may name it until its seam lands, so wildcards expand over
        this set rather than over :attr:`verbs`.
        """

        return self.verbs_for(resource_type) - self.reserved.get(resource_type, frozenset())


CATALOGUE_V1: Final = VerbCatalogue(
    version="v1",
    verbs={
        "repo": frozenset({"read", "branch", "push", "pr.open", "pr.review", "pr.merge"}),
        # `work.accept` (PRD issue 29): whether a Work Record dispatched to an Agent that
        # did not create it is taken unattended, asked about, or refused. Amended into v1
        # (not a new version) because the seam it names -- the Runner's own dispatch --
        # already exists; a Grant written before this landed simply carries no entry for
        # it and reads the Contract-funding default (`services.grants.work_accept`).
        "work": frozenset({"work.accept"}),
    },
    reserved={"repo": frozenset({"pr.comment"})},
)

# v2 adds the Channel (PRD issue 51, ADR-0012 s1): a Swarm's standing definition is a
# Product resource, and membership *is* the Grant -- an Agent is a member of a Channel iff
# its Effective Grant allows `channel.read` on it, and may speak iff `channel.write`. Both
# verbs were reserved until the seam that enforces them landed: the Message store's
# `send` / `list` callbacks (PRD issue 52, `agentic_runner.activities`), which evaluate
# `(channel, <selector>, write | read)` against the sending Agent's Effective Grant.
CATALOGUE_V2: Final = VerbCatalogue(
    version="v2",
    verbs={
        **CATALOGUE_V1.verbs,
        "channel": frozenset({"read", "write"}),
    },
    reserved=dict(CATALOGUE_V1.reserved),
)

# v3 lifts the reservation on `pr.comment` (PRD issue 54): the Runner's `post_pr_review`
# activity is its seam, evaluating `(repo, <repo>, pr.comment)` against the Critic's
# Effective Grant before it posts the Critic's review. A new version rather than an
# amendment, so a Grant saved with a `*` under v2 is not silently widened by it.
CATALOGUE_V3: Final = VerbCatalogue(
    version="v3",
    verbs={
        **CATALOGUE_V2.verbs,
        "repo": CATALOGUE_V2.verbs["repo"] | {"pr.comment"},
    },
    reserved={},
)

# v4 makes ``mcp:<server>`` live (PRD issue 58): the Runner's MCP config assembly is its
# seam, writing a registered server into the CLI's config only when the Effective Grant
# allows it. The platform verbs are unchanged; what v4 adds is that an ``mcp:`` entry's
# verb wildcards expand over the *server's* registered tool names when the Grant is
# saved, so a server that later registers a new tool widens nobody who did not re-save.
CATALOGUE_V4: Final = VerbCatalogue(
    version="v4",
    verbs=dict(CATALOGUE_V3.verbs),
    reserved={},
    expands_mcp=True,
)

# v5 adds the Epic verbs (PRD issue 59, ADR-0011 amended by ticket 19): `work.open` on
# the `channel` resource ("may open Work Records on Channel X") and `work.end` on `work`.
# Their seam is the control plane's plan evaluation the Runner's dispatch relies on --
# the same seam shape as `work.accept` -- and like `work.accept` both read a default
# when a Grant carries no entry: `work.open` from the Contract's funding (org-funded
# `allow`, user-funded `confirm`), `work.end` `allow`. A new version rather than an
# amendment so a `*` on a Channel saved under v4 is not silently widened to open work.
# `work.ask` (PRD issue 60, 19 A9) is amended into v5 rather than a v6: v5 has shipped to
# no deployment yet (it lives on the Release 1 integration branch with issue 59), its
# seam is the control plane's Question raise the Runner's `ask` callback relies on, and
# like the other `work.*` verbs it reads a default (`allow`) when no entry names it.
CATALOGUE_V5: Final = VerbCatalogue(
    version="v5",
    verbs={
        **CATALOGUE_V4.verbs,
        "channel": CATALOGUE_V4.verbs["channel"] | {"work.open"},
        "work": CATALOGUE_V4.verbs["work"] | {"work.end", "work.ask"},
    },
    reserved={},
    expands_mcp=True,
)

CURRENT_CATALOGUE_VERSION: Final = CATALOGUE_V5.version

_CATALOGUES: Final[dict[str, VerbCatalogue]] = {
    CATALOGUE_V1.version: CATALOGUE_V1,
    CATALOGUE_V2.version: CATALOGUE_V2,
    CATALOGUE_V3.version: CATALOGUE_V3,
    CATALOGUE_V4.version: CATALOGUE_V4,
    CATALOGUE_V5.version: CATALOGUE_V5,
}

# ``mcp:<server>`` entries carry the *server's* tool names as verbs, not platform
# vocabulary, so the catalogue never lists them: the server registry does. Derived at
# read time from ``Persona.mcp_grants`` for the Persona link
# (``grants.model.persona_allow_list``), never copied into a column, so the slugs
# cannot drift from the Grant that quotes them.
MCP_RESOURCE_TYPE_PREFIX: Final = "mcp:"


def catalogue_for(version: str) -> VerbCatalogue:
    try:
        return _CATALOGUES[version]
    except KeyError as error:
        raise UnknownCatalogueVersionError(f"unknown verb catalogue version {version!r}") from error


def is_mcp_resource_type(resource_type: str) -> bool:
    return resource_type.startswith(MCP_RESOURCE_TYPE_PREFIX)


def mcp_resource_type(server_slug: str) -> str:
    return f"{MCP_RESOURCE_TYPE_PREFIX}{server_slug}"
