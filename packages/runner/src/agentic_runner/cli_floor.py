"""Which executable each Agent Runtime CLI is, and the version floor it must clear."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Final

__all__ = ["CLI_FLOORS", "CLI_NAMES", "meets_floor", "parse_cli_version"]

# What the Profile's `cli_kind` runs, by executable name on PATH.
CLI_NAMES: Final[Mapping[str, str]] = {"codex_cli": "codex", "claude_code": "claude"}
# The versions the Runner image is built and tested against (Dockerfile.runner's
# CODEX_CLI_VERSION / CLAUDE_CODE_VERSION; a test holds the two together). Below the
# floor is *reported*, not refused: the user's own CLI is the user's to upgrade.
CLI_FLOORS: Final[Mapping[str, str]] = {"codex": "0.141.0", "claude": "2.1.280"}

_VERSION = re.compile(r"(\d+\.\d+\.\d+[0-9A-Za-z.+-]{0,40})")


def parse_cli_version(output: str) -> str | None:
    match = _VERSION.search(output)
    return None if match is None else match.group(1)


def meets_floor(name: str, version: str) -> bool:
    """Whether ``version`` of the executable ``name`` clears the Runner's floor for it."""

    return _version_tuple(version) >= _version_tuple(CLI_FLOORS[name])


def _version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", version)[:3])
