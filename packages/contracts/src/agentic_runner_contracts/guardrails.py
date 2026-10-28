"""The Guardrails one Directive runs under (Spec: Agent Guardrails rev 7).

What the Agent may touch on the machine: commands, network destinations, folders and
limits. The platform compiles them per Directive from six layers -- the platform floor,
the Organisation (one per Agent Runtime), the Product, the owner's Teams, the Agent and
the Runner -- and the Runner enforces the result it is sent. The Runner never compiles,
widens or narrows them: it is untrusted, because its host controls the machine.

The Organisation layer allows and every other layer only removes, so the compiled result
is three allow-lists, the limits, and every entry a narrowing layer removed with that
layer. An empty allow-list is the Runner's own default -- its built-in command policy,
any destination, the whole Workspace -- and an entry in ``removed`` is refused even then.
A refusal names the layer that refused it: the one in ``removed``, else the Organisation,
whose list did not allow it.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

GUARDRAILS_EVIDENCE_SOURCE: Final = "runner.guardrails"
GUARDRAILS_REFUSED: Final = "guardrails.refused"

GUARDRAIL_ENTRIES_MAX: Final = 256

Entry = Annotated[str, StringConstraints(min_length=1, max_length=1024)]


class GuardrailLayer(StrEnum):
    PLATFORM_FLOOR = "platform_floor"
    ORGANISATION = "organisation"
    PRODUCT = "product"
    TEAM = "team"
    AGENT = "agent"
    RUNNER = "runner"


class GuardrailPart(StrEnum):
    COMMANDS = "commands"
    NETWORK = "network"
    FOLDERS = "folders"
    LIMITS = "limits"


class GuardrailLimitName(StrEnum):
    MEMORY_LIMIT_BYTES = "memory_limit_bytes"
    WALL_CLOCK_SECONDS = "wall_clock_seconds"


# How a refusal reads where a person sees it: "Refused by the Product's Guardrails".
_LAYER_LABELS: Final = {
    GuardrailLayer.PLATFORM_FLOOR: "the platform floor",
    GuardrailLayer.ORGANISATION: "the Organisation's Guardrails",
    GuardrailLayer.PRODUCT: "the Product's Guardrails",
    GuardrailLayer.TEAM: "the Team's Guardrails",
    GuardrailLayer.AGENT: "the Agent's Guardrails",
    GuardrailLayer.RUNNER: "the Runner's Guardrails",
}


class RemovedEntry(BaseModel):
    """One entry a layer took away, and that layer."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    part: Literal[GuardrailPart.COMMANDS, GuardrailPart.NETWORK, GuardrailPart.FOLDERS]
    entry: Entry
    layer: GuardrailLayer


class GuardrailLimit(BaseModel):
    """The smallest value any layer set, and the layer that set it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    value: int = Field(gt=0)
    layer: GuardrailLayer


class Guardrails(BaseModel):
    """The compiled Guardrails, as ``WorkerRuntimeContext.guardrails`` carries them.

    ``commands`` entries are a program name, or a program and the arguments a command must
    start with (``npm test``). ``network`` entries are a host, ``host:port`` or
    ``*.domain``. ``folders`` are paths inside the Workspace, relative to its root, and a
    removed folder is a path anywhere (``~/`` and ``$HOME`` expand on the Runner).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    commands: tuple[Entry, ...] = Field(default=(), max_length=GUARDRAIL_ENTRIES_MAX)
    network: tuple[Entry, ...] = Field(default=(), max_length=GUARDRAIL_ENTRIES_MAX)
    folders: tuple[Entry, ...] = Field(default=(), max_length=GUARDRAIL_ENTRIES_MAX)
    limits: dict[GuardrailLimitName, GuardrailLimit] = Field(default_factory=dict)
    # What a runtime asked per command does with one the command policy has no answer
    # for (PRD decision 12). Set only on the Organisation layer, per Agent Runtime.
    permission_fallback: Literal["deny", "hold"] = "deny"
    removed: tuple[RemovedEntry, ...] = Field(default=(), max_length=4 * GUARDRAIL_ENTRIES_MAX)

    def removed_from(self, part: GuardrailPart) -> tuple[RemovedEntry, ...]:
        return tuple(removed for removed in self.removed if removed.part == part)

    def limit(self, name: GuardrailLimitName) -> GuardrailLimit | None:
        return self.limits.get(name)


def describe_refusal(*, part: GuardrailPart, entry: str, layer: GuardrailLayer) -> str:
    """``Refused by the Product's Guardrails: network `db.prod.internal```."""

    return f"Refused by {_LAYER_LABELS[layer]}: {part.value} `{entry}`"


def refusal_evidence(
    *, part: GuardrailPart, entry: str, layer: GuardrailLayer, **detail: object
) -> dict[str, object]:
    """The Evidence payload a refusal writes under :data:`GUARDRAILS_EVIDENCE_SOURCE`."""

    return {
        "event": GUARDRAILS_REFUSED,
        "part": part.value,
        "entry": entry,
        "layer": layer.value,
        "summary": describe_refusal(part=part, entry=entry, layer=layer),
        **detail,
    }
