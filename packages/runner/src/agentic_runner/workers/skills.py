"""The Agent's attached Skills, delivered to its Agent Runtime for one Directive.

Console-v2 issue 28, ADR-0017 §1. Where each runtime reads a Skill was measured against the
Runner's own argv, not assumed (local-agents 02 asked the same question over ACP):

- **Codex** (codex-cli 0.159.2): ``$CODEX_HOME/skills/<slug>/SKILL.md`` is advertised to the
  model under ``codex exec``, with and without ``--ignore-user-config``. The Runner writes
  there, in the Contract's harness root, and removes it after the Directive.
- **Claude Code** (2.1.292): the Runner runs ``claude --print --bare``, and ``--bare`` drops
  the Skill tool. ``$CLAUDE_CONFIG_DIR/skills/<slug>/SKILL.md`` is then neither advertised
  nor resolved by ``/<slug>``, and ``--tools …,Skill`` does not bring the tool back. Claude
  Code therefore gets the bodies as a prompt preamble. Dropping ``--bare`` would also
  re-enable the Workspace's hooks and ``CLAUDE.md``, which is not this module's call.

The harness root is per Contract, not per Directive, so two concurrent Directives of one
Contract share its ``skills`` directory.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from collections.abc import Sequence
from enum import StrEnum
from pathlib import Path
from typing import Final

from agentic_runner_contracts.runtime_context import SkillVersionSpec

__all__ = [
    "SKILLS_DIR",
    "SkillDelivery",
    "SkillDeliveryError",
    "delivery_for",
    "first_digest_mismatch",
    "remove_skills",
    "skills_preamble",
    "write_skills",
]

SKILLS_DIR: Final[str] = "skills"


class SkillDelivery(StrEnum):
    DIRECTORY = "directory"
    PROMPT_PREAMBLE = "prompt_preamble"


_DELIVERY_BY_CLI_KIND: Final[dict[str, SkillDelivery]] = {
    "codex_cli": SkillDelivery.DIRECTORY,
    "claude_code": SkillDelivery.PROMPT_PREAMBLE,
}


class SkillDeliveryError(RuntimeError):
    """Raised when the Skills could not be written into, or removed from, the harness root."""


def delivery_for(cli_kind: str) -> SkillDelivery:
    return _DELIVERY_BY_CLI_KIND.get(cli_kind, SkillDelivery.PROMPT_PREAMBLE)


def first_digest_mismatch(skills: Sequence[SkillVersionSpec]) -> SkillVersionSpec | None:
    for skill in skills:
        if hashlib.sha256(skill.body.encode("utf-8")).hexdigest() != skill.sha256:
            return skill
    return None


def skills_preamble(skills: Sequence[SkillVersionSpec]) -> str:
    """The bodies, for a runtime with no skills directory. Empty Skills, empty preamble,
    so a Directive without Skills keeps a byte-identical prompt."""

    if not skills:
        return ""
    sections = "".join(
        f"Skill {skill.slug} (version {skill.version}):\n{skill.body.strip()}\n\n"
        for skill in skills
    )
    return (
        "The Skills below are attached to you. Follow each one where it applies.\n\n"
        f"{sections}"
        "The pipeline rules take precedence over the Skills above: edit files only, do not "
        "run git commit or git push, and do not open a pull request — the pipeline commits, "
        "pushes, and manages the PR.\n\n"
    )


# Both run as the Contract's uid, never the Runner's. The Contract owns its harness root, so
# an Agent could have left a symlink there; a write or an rmtree by the Runner -- with
# CAP_CHOWN and CAP_DAC_OVERRIDE -- would follow it out of the tree. As the Contract's uid
# it can only reach what the Contract already could.
_WRITE_SCRIPT: Final[str] = """
import json, os, sys
root, skills = json.load(sys.stdin)
os.makedirs(root, mode=0o700, exist_ok=True)
for slug, body in skills:
    directory = os.path.join(root, slug)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    path = os.path.join(directory, "SKILL.md")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW
    with os.fdopen(os.open(path, flags, 0o600), "w", encoding="utf-8") as handle:
        handle.write(body)
"""

_REMOVE_SCRIPT: Final[str] = """
import json, os, shutil, sys
root, slugs = json.load(sys.stdin)
for slug in slugs:
    shutil.rmtree(os.path.join(root, slug), ignore_errors=True)
left = [slug for slug in slugs if os.path.lexists(os.path.join(root, slug))]
if left:
    sys.exit("not removed: " + ", ".join(left))
"""


def _skills_root(harness_config_dir: Path) -> str:
    return str(harness_config_dir / SKILLS_DIR)


async def write_skills(
    harness_config_dir: Path, skills: Sequence[SkillVersionSpec], *, uid: int | None
) -> None:
    """Write each Skill as ``<harness>/skills/<slug>/SKILL.md``, as the Contract's uid."""

    await _run_as(
        uid,
        _WRITE_SCRIPT,
        [_skills_root(harness_config_dir), [[skill.slug, skill.body] for skill in skills]],
    )


async def remove_skills(
    harness_config_dir: Path, skills: Sequence[SkillVersionSpec], *, uid: int | None
) -> None:
    """Remove what :func:`write_skills` wrote. The ``skills`` directory itself stays:
    Codex keeps its own ``.system`` Skills there."""

    await _run_as(
        uid,
        _REMOVE_SCRIPT,
        [_skills_root(harness_config_dir), [skill.slug for skill in skills]],
    )


async def _run_as(uid: int | None, script: str, payload: object) -> None:
    identity: dict[str, object] = (
        {} if uid is None else {"user": uid, "group": uid, "extra_groups": []}
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        "-c",
        script,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        **identity,  # type: ignore[arg-type]
    )
    _, stderr = await process.communicate(json.dumps(payload).encode("utf-8"))
    if process.returncode != 0:
        raise SkillDeliveryError(stderr.decode("utf-8", errors="replace").strip()[-500:])
