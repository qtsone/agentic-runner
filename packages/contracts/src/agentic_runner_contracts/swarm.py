"""The Swarm a Work Record runs as, and the scheduler's wake rules (PRD issue 53).

ADR-0012 §1: a Swarm is the Agents holding Roles on one Work Record, instantiated from
the Channel's standing defaults at dispatch **as Evidence** and carried by the workflow
as deterministic state -- plus any participants its dispatch named, who hold no Role but
can be woken (console-v2 issue 22). This module is that state and the rules over it, shared by the
workflow (which schedules on them), the Runner (which refuses a non-Lead's `pr.*` verbs
on them) and the console (which shows who is woken) -- one place, so the three cannot
disagree about who a `request` wakes.

Sandbox-safe on purpose: stdlib only, imported by ``workflows/ralph.py``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final

__all__ = [
    "PARTICIPANT_ADDED_EVENT",
    "PARTICIPANT_REMOVED_EVENT",
    "ROLE_ASSIGNED_EVENT",
    "ROLE_BUILDER",
    "ROLE_CRITIC",
    "ROLE_LEAD",
    "ROLE_LIAISON",
    "ROLES",
    "SWARM_SOURCE",
    "SwarmSnapshot",
    "swarm_from_payload",
    "wake_target",
]

# The Evidence stream the Swarm is folded from: one ``swarm.role_assigned`` Event per
# assignment -- the Channel's defaults at dispatch, a Lead the user chose instead, a
# `handoff` at run time. The last assignment of a role wins.
SWARM_SOURCE: Final = "swarm"
ROLE_ASSIGNED_EVENT: Final = "swarm.role_assigned"
# A participant -- a Channel member who takes part and can be woken but holds no Role --
# joins and leaves on the same stream, in order; the last of the two for an Agent wins.
PARTICIPANT_ADDED_EVENT: Final = "swarm.participant_added"
PARTICIPANT_REMOVED_EVENT: Final = "swarm.participant_removed"

ROLE_LEAD: Final = "lead"
ROLE_LIAISON: Final = "liaison"
ROLE_BUILDER: Final = "builder"
ROLE_CRITIC: Final = "critic"
ROLES: Final = (ROLE_LEAD, ROLE_LIAISON, ROLE_BUILDER, ROLE_CRITIC)


@dataclass(frozen=True)
class SwarmSnapshot:
    """``role -> Agent id`` on one Work Record, folded from its role-assignment Evidence,
    and the participants beside them, who hold no Role and so no `pr.*` capability.

    The empty snapshot is the **degenerate Swarm**: no Channel, so the Work Record's own
    Agent holds every role and every Directive names no member -- exactly the one-Agent
    loop that predates Channels, which is why nothing migrates.
    """

    channel_id: str = ""
    roles: dict[str, str] = field(default_factory=dict)
    participants: tuple[str, ...] = ()

    @property
    def lead(self) -> str:
        return self.roles.get(ROLE_LEAD, "")

    @property
    def members(self) -> tuple[str, ...]:
        """Role holders first, then participants, each once."""

        return tuple(dict.fromkeys((*self.roles.values(), *self.participants)))

    def holder(self, role: str) -> str:
        return self.roles.get(role, "")

    def roles_of(self, agent_id: str) -> tuple[str, ...]:
        return tuple(role for role, holder in self.roles.items() if holder == agent_id)

    def with_role(self, role: str, agent_id: str) -> SwarmSnapshot:
        return SwarmSnapshot(
            channel_id=self.channel_id,
            roles={**self.roles, role: agent_id},
            participants=self.participants,
        )


def swarm_from_payload(payload: Mapping[str, Any]) -> SwarmSnapshot:
    """Parse ``GET /api/internal/work-records/{id}/swarm``."""

    roles = payload.get("roles")
    participants = payload.get("participants")
    return SwarmSnapshot(
        channel_id=str(payload.get("channel_id") or ""),
        roles=(
            {str(role): str(agent_id) for role, agent_id in roles.items() if agent_id}
            if isinstance(roles, Mapping)
            else {}
        ),
        participants=(
            tuple(str(agent_id) for agent_id in participants if agent_id)
            if isinstance(participants, list | tuple)
            else ()
        ),
    )


def wake_target(
    swarm: SwarmSnapshot,
    *,
    kind: str,
    recipient_agent_id: str = "",
    recipient_role: str = "",
) -> str:
    """Which member one Message wakes, or ``""`` for none (ADR-0012 §1, map ticket 10).

    `request` wakes its addressee (an Agent -- a Role holder or a participant -- or
    whichever Agent holds the named role);
    `result` wakes the requester it names; `handoff` wakes the new holder; `verdict` wakes
    the Lead; `note` wakes nobody and rides with the recipient's next Directive. An
    addressee the Swarm cannot resolve falls to the Lead, who decides -- never to nobody,
    which is how a loop stalls with work outstanding.
    """

    if kind == "note":
        return ""
    if kind == "verdict":
        return swarm.lead
    if recipient_agent_id in swarm.members:
        return recipient_agent_id
    if recipient_role:
        return swarm.holder(recipient_role) or swarm.lead
    return swarm.lead
