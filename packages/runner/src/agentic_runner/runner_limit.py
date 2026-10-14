"""The Runner Limit as this process applies it (ADR-0022 §52-§58).

``RunnerConfig.limits`` is the host's three deny-lists as written. :meth:`AppliedRunnerLimit.
from_config` checks them once, at start, and refuses to start on one it cannot apply -- a
verb the pinned Verb Catalogue does not know, a destination that is not one, a folder that
holds the Workspace root -- rather than let the host believe a limit holds that does not.
What it builds is what every check reads: the tools part as the evaluator's
``runner_limit`` link, the networks part for the egress proxy, and the folders part,
resolved, for the command policy and the ACP permission seam.

A deny-list fails open where its matching misses, so :class:`DeniedFolders` errs the other
way: both sides are resolved (symlinks, ``..``, ``~``, ``$HOME``), compared by whole path
components and case-folded on a case-insensitive filesystem, and an argument it cannot
resolve -- an unexpanded variable, a glob that could reach a denied folder -- is refused.
A refusal names the entry by its position, ``folders[2]``, never by its path: Evidence is
read by people the console hides a person's folder names from (§57).
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Final

from agentic_runner.egress import parse_destination
from agentic_runner_contracts.grants.catalogue import (
    CURRENT_CATALOGUE_VERSION,
    VerbCatalogue,
    catalogue_for,
    is_mcp_resource_type,
)
from agentic_runner_contracts.grants.model import Grant
from agentic_runner_contracts.runner_limit import RunnerLimit
from agentic_runner_contracts.runner_registration import (
    EgressPosture,
    FolderPosture,
    RunnerLimitPosture,
    RunnerLimitReport,
)

__all__ = [
    "EMPTY_RUNNER_LIMIT",
    "RUNNER_LIMIT_EVIDENCE_SOURCE",
    "AppliedRunnerLimit",
    "DeniedFolders",
    "RunnerLimitError",
    "case_insensitive_filesystem",
    "expand_home",
]

RUNNER_LIMIT_EVIDENCE_SOURCE: Final[str] = "runner.runner_limit"

# `$NAME` or `${NAME}`: what a shell would replace and this check cannot know the value of.
_VARIABLE: Final[re.Pattern[str]] = re.compile(r"\$(\{[^}]*\}|[A-Za-z_][A-Za-z0-9_]*)")
_HOME_VARIABLE: Final[re.Pattern[str]] = re.compile(r"\$(\{HOME\}|HOME(?![A-Za-z0-9_]))")
_GLOB: Final[re.Pattern[str]] = re.compile(r"[*?\[]")
# What splits a shell script into words, for a permission request the policy cannot parse.
_SHELL_WORD_BREAK: Final[re.Pattern[str]] = re.compile(r"[\s;&|<>()`'\"]+")


class RunnerLimitError(ValueError):
    """The Runner Limit cannot be applied as written; the Runner refuses to start."""


def expand_home(text: str, home: str | None = None) -> str:
    """``~``, ``~user``, ``$HOME`` and ``${HOME}`` expanded; nothing else."""

    home = home if home is not None else os.path.expanduser("~")
    text = _HOME_VARIABLE.sub(lambda _: home, text)
    if text == "~" or text.startswith("~/"):
        return home + text[1:]
    return os.path.expanduser(text) if text.startswith("~") else text


@dataclass(frozen=True)
class _Folder:
    # The entry as written and as resolved: a symlinked entry denies the link's path and
    # its target, so a path reaching it either way is refused.
    forms: tuple[tuple[str, ...], ...]
    case_insensitive: bool

    def holds(self, parts: tuple[str, ...]) -> bool:
        compared = _fold(parts, self.case_insensitive)
        return any(compared[: len(form)] == form for form in self.forms)

    def is_under(self, parts: tuple[str, ...]) -> bool:
        """Whether ``parts`` is this folder or one of its ancestors: a glob rooted there
        could reach into it."""

        compared = _fold(parts, self.case_insensitive)
        return any(form[: len(compared)] == compared for form in self.forms)


def _fold(parts: tuple[str, ...], case_insensitive: bool) -> tuple[str, ...]:
    return tuple(part.casefold() for part in parts) if case_insensitive else parts


def _forms(path: str) -> tuple[Path, ...]:
    lexical = Path(os.path.normpath(path))
    return tuple(dict.fromkeys((lexical, Path(os.path.realpath(path)))))


def case_insensitive_filesystem(path: Path) -> bool:
    """Whether the filesystem holding ``path`` folds case.

    Asked of the nearest existing ancestor with a cased name: its other-case spelling names
    the same file only on a folding filesystem. Where no such ancestor exists, the platform's
    default answers (macOS and Windows fold).
    """

    for candidate in (path, *path.parents):
        name = candidate.name
        if name == name.swapcase() or not candidate.exists():
            continue
        other = candidate.with_name(name.swapcase())
        try:
            return other.exists() and os.path.samefile(candidate, other)
        except OSError:
            return False
    return sys.platform in {"darwin", "win32"}


class DeniedFolders:
    """The Runner Limit's folders, resolved once at start, as the path checks read them."""

    def __init__(
        self,
        folders: Sequence[str],
        *,
        home: str | None = None,
        case_insensitive: Callable[[Path], bool] = case_insensitive_filesystem,
    ) -> None:
        self._home = home if home is not None else os.path.expanduser("~")
        self._folders = tuple(
            _Folder(
                forms=tuple(
                    _fold(form.parts, folds) for form in _forms(expand_home(folder, self._home))
                ),
                case_insensitive=folds,
            )
            for folder in folders
            for folds in (case_insensitive(Path(expand_home(folder, self._home))),)
        )
        self._written = tuple(folders)

    def __bool__(self) -> bool:
        return bool(self._folders)

    def holding(self, path: Path) -> int | None:
        """The position of the first entry that is ``path`` or one of its ancestors."""

        for form in _forms(str(path)):
            for index, folder in enumerate(self._folders):
                if folder.holds(form.parts):
                    return index
        return None

    def refusal(self, words: Iterable[str], cwd: Path) -> str | None:
        """``folders[<n>]`` for the first word that could reach a denied folder, or None.

        ``folders[*]`` when it is not known which: a word with a variable left in it.
        Every word is read as a possible path -- relative ones from ``cwd`` -- and so is the
        value after an ``=`` and the path glued to a short option (``-I/etc``): a name that
        is not a path resolves inside the Workspace, which no entry may hold.
        """

        if not self._folders:
            return None
        for word in words:
            for candidate in _candidates(word):
                found = self._candidate_refusal(candidate, cwd)
                if found is not None:
                    return found
        return None

    def script_refusal(self, script: str, cwd: Path) -> str | None:
        """As :meth:`refusal`, for a shell script the policy cannot parse into an argv."""

        return self.refusal((script, *_SHELL_WORD_BREAK.split(script)), cwd)

    def claude_permission_deny(self) -> list[str]:
        """Claude Code ``permissions.deny`` rules for every form of every entry.

        ``//`` is Claude Code's spelling of an absolute path in a permission rule.
        """

        rules: list[str] = []
        for folder in self._written:
            for form in _forms(expand_home(folder, self._home)):
                path = form.as_posix().rstrip("/")
                for tool in ("Read", "Edit"):
                    rules.extend((f"{tool}(/{path})", f"{tool}(/{path}/**)"))
        return list(dict.fromkeys(rules))

    def _candidate_refusal(self, candidate: str, cwd: Path) -> str | None:
        expanded = expand_home(candidate, self._home)
        # A NUL ends the path where the kernel reads it and is refused by `realpath`, so no
        # resolution here can say what it names.
        if _VARIABLE.search(expanded) or "\x00" in expanded:
            return "folders[*]"
        if _GLOB.search(expanded):
            return self._glob_refusal(expanded, cwd)
        index = self.holding(cwd / expanded)
        return None if index is None else f"folders[{index}]"

    def _glob_refusal(self, pattern: str, cwd: Path) -> str | None:
        """A glob is refused when the directory it starts from holds, or is held by, an
        entry: it could match its way into the folder whatever its tail says."""

        parts = Path(cwd / pattern).parts
        first = next(i for i, part in enumerate(parts) if _GLOB.search(part))
        literal = Path(*parts[:first]) if first else Path(parts[0] if parts else "/")
        for form in _forms(str(literal)):
            for index, folder in enumerate(self._folders):
                if folder.holds(form.parts) or folder.is_under(form.parts):
                    return f"folders[{index}]"
        return None


def _candidates(word: str) -> tuple[str, ...]:
    word = word.strip()
    if not word:
        return ()
    candidates = [word]
    if "=" in word:
        candidates.append(word.split("=", 1)[1])
    if word.startswith("-"):
        glued = re.search(r"[/~]", word)
        if glued is not None:
            candidates.append(word[glued.start() :])
    if "://" in word:
        # A URL is not a path, except one naming a local file.
        candidates = [c.removeprefix("file://") for c in candidates if c.startswith("file://")]
    return tuple(candidate for candidate in candidates if candidate)


@dataclass(frozen=True)
class AppliedRunnerLimit:
    """The Runner Limit, checked and ready for every check that reads it."""

    limit: RunnerLimit
    folders: DeniedFolders

    @classmethod
    def from_config(
        cls,
        limit: RunnerLimit,
        *,
        workspace_roots: Iterable[Path],
        catalogue: VerbCatalogue | None = None,
        home: str | None = None,
        case_insensitive: Callable[[Path], bool] = case_insensitive_filesystem,
    ) -> AppliedRunnerLimit:
        """Check the limit against this Runner; raise :class:`RunnerLimitError` naming the
        first entry it cannot apply."""

        _check_tools(limit, catalogue or catalogue_for(CURRENT_CATALOGUE_VERSION))
        for index, entry in enumerate(limit.networks):
            try:
                parse_destination(entry)
            except ValueError as error:
                raise RunnerLimitError(f"Runner Limit networks[{index}]: {error}") from None
        folders = DeniedFolders(limit.folders, home=home, case_insensitive=case_insensitive)
        for root in workspace_roots:
            holder = folders.holding(root)
            if holder is not None:
                raise RunnerLimitError(
                    f"Runner Limit folders[{holder}] contains the Workspace root {root}, so no "
                    "Directive could run; deny a folder beside it instead"
                )
        return cls(limit=limit, folders=folders)

    @property
    def empty(self) -> bool:
        return not (self.limit.tools or self.limit.networks or self.limit.folders)

    @property
    def tools_grant(self) -> Grant | None:
        """The evaluator's ``runner_limit`` link; None leaves the chain exactly as it was."""

        return self.limit.tools_grant if self.limit.tools else None

    @property
    def networks(self) -> tuple[str, ...]:
        return self.limit.networks

    def report(
        self, *, egress_posture: EgressPosture, folders: Mapping[str, FolderPosture]
    ) -> RunnerLimitReport | None:
        """What registration and the heartbeat carry; None while the limit is empty.

        Omitted while empty, as the envelope omits every field added after a control plane
        still in service: that control plane refuses a body naming a field it does not
        know, and a Runner that limits nothing would lose its heartbeat for no gain. A
        host who writes a limit needs a control plane on contracts 3.21 first.
        """

        if self.empty:
            return None
        return RunnerLimitReport(
            limit=self.limit,
            posture=RunnerLimitPosture(networks=egress_posture, folders=dict(folders)),
        )


EMPTY_RUNNER_LIMIT: Final[AppliedRunnerLimit] = AppliedRunnerLimit(
    limit=RunnerLimit(), folders=DeniedFolders(())
)


def _check_tools(limit: RunnerLimit, catalogue: VerbCatalogue) -> None:
    for index, entry in enumerate(limit.tools):
        if is_mcp_resource_type(entry.resource_type):
            # An MCP server's verbs are its registered tool names, which the catalogue
            # never lists: the server registry does.
            continue
        if not catalogue.knows(entry.resource_type):
            raise RunnerLimitError(
                f"Runner Limit tools[{index}] names resource type {entry.resource_type!r}, "
                f"which Verb Catalogue {catalogue.version} does not have"
            )
        known = catalogue.verbs_for(entry.resource_type)
        for pattern in entry.verbs:
            if not any(fnmatchcase(verb, pattern) for verb in known):
                raise RunnerLimitError(
                    f"Runner Limit tools[{index}] names verb {pattern!r}, which "
                    f"{entry.resource_type!r} does not have in Verb Catalogue "
                    f"{catalogue.version}"
                )
