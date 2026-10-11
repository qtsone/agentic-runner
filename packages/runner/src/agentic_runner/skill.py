"""``agentic-runner skill install | print``: the setup skill for the person's own coding CLI.

The skill (console-v2 issue 21) is read and run by the person's own Claude Code or Codex,
so the control plane calls no LLM to onboard anyone (ADR-0010 §4). It ships inside the
wheel so ``uvx agentic-runner skill install`` needs nothing but PyPI, and the copy a CLI
reads is always the one that matches the Runner it installs.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from importlib.resources import files
from pathlib import Path
from typing import Literal

SKILL_NAME = "agentic-runner-setup"

Target = Literal["claude", "codex"]
TARGETS: tuple[Target, ...] = ("claude", "codex")


def text() -> str:
    return files("agentic_runner").joinpath("skills", SKILL_NAME, "SKILL.md").read_text("utf-8")


def skill_dir(target: Target, *, home: Path, environ: Mapping[str, str]) -> Path:
    """Where each CLI reads a user skill from.

    Claude Code honours ``CLAUDE_CONFIG_DIR`` for its whole user directory; Codex reads user
    skills from ``~/.agents/skills``, not from ``CODEX_HOME``.
    """

    if target == "claude":
        root = Path(environ.get("CLAUDE_CONFIG_DIR") or home / ".claude")
    else:
        root = home / ".agents"
    return root / "skills" / SKILL_NAME


def install(
    targets: tuple[Target, ...],
    *,
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> list[Path]:
    written = []
    for target in targets:
        directory = skill_dir(
            target,
            home=home or Path.home(),
            environ=os.environ if environ is None else environ,
        )
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "SKILL.md"
        path.write_text(text(), encoding="utf-8")
        written.append(path)
    return written
