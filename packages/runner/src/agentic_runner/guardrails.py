"""A Directive's Guardrails as this process checks them (Spec: Agent Guardrails rev 7).

The platform compiles the Guardrails and sends them on the runtime context; the Runner
never compiles, widens or narrows them. :class:`DirectiveGuardrails` is that result made
ready for the checks that read it: the commands part for the command policy, the folders
part for the command policy and the ACP permission seam, and the network part for the
attempt's egress proxy. Every refusal is a :class:`GuardrailRefusal` naming the layer that
refused -- the one that removed the entry, else the Organisation, whose list did not allow
it -- and becomes one Evidence Event.

A deny-list fails open where its matching misses, so :class:`DeniedFolders` errs the other
way: both sides are resolved (symlinks, ``..``, ``~``, ``$HOME``), compared by whole path
components and case-folded on a case-insensitive filesystem, and an argument it cannot
resolve -- an unexpanded variable, a glob that could reach a denied folder -- is refused.
An allow-list fails the other way, so the folders the Organisation allows are read only
over what plainly names a path inside the Workspace: an argument that merely could be one
is no reason to refuse a command.
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from agentic_runner.egress import destination_denied
from agentic_runner_contracts.guardrails import (
    GuardrailLayer,
    GuardrailLimitName,
    GuardrailPart,
    Guardrails,
    describe_refusal,
    refusal_evidence,
)

__all__ = [
    "ANY_FOLDER",
    "DeniedFolders",
    "DirectiveGuardrails",
    "GuardrailRefusal",
    "case_insensitive_filesystem",
    "expand_home",
    "turn_timeout",
]

# `$NAME` or `${NAME}`: what a shell would replace and this check cannot know the value of.
_VARIABLE: Final[re.Pattern[str]] = re.compile(r"\$(\{[^}]*\}|[A-Za-z_][A-Za-z0-9_]*)")
_HOME_VARIABLE: Final[re.Pattern[str]] = re.compile(r"\$(\{HOME\}|HOME(?![A-Za-z0-9_]))")
_GLOB: Final[re.Pattern[str]] = re.compile(r"[*?\[]")
# What splits a shell script into words, for a permission request the policy cannot parse.
_SHELL_WORD_BREAK: Final[re.Pattern[str]] = re.compile(r"[\s;&|<>()`'\"]+")
# A path glued inside one word: `curl -d @/x`, `docker -v /x:/mnt`, `scp host:/x`, `a,/x`.
_PATH_AFTER_SEPARATOR: Final[re.Pattern[str]] = re.compile(r"(?<=[@:,=])[/~]")
_LIST_SEPARATOR: Final[re.Pattern[str]] = re.compile(r"[:,]")

# :meth:`DeniedFolders.refusal`'s answer when it is not known which entry a word could
# reach: a word with a variable left in it.
ANY_FOLDER: Final[int] = -1


@dataclass(frozen=True, slots=True)
class GuardrailRefusal:
    """One thing a Directive's Guardrails refused, and the layer that refused it."""

    part: GuardrailPart
    entry: str
    layer: GuardrailLayer

    @property
    def summary(self) -> str:
        return describe_refusal(part=self.part, entry=self.entry, layer=self.layer)

    def evidence(self, **detail: object) -> dict[str, object]:
        return refusal_evidence(part=self.part, entry=self.entry, layer=self.layer, **detail)


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
    """Folders no command may reach, resolved once, as the path checks read them."""

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

    def refusal(self, words: Iterable[str], cwd: Path) -> int | None:
        """The position of the entry the first word could reach, or None.

        :data:`ANY_FOLDER` when it is not known which: a word with a variable left in it.
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

    def _candidate_refusal(self, candidate: str, cwd: Path) -> int | None:
        expanded = expand_home(candidate, self._home)
        # A NUL ends the path where the kernel reads it and is refused by `realpath`, so no
        # resolution here can say what it names.
        if _VARIABLE.search(expanded) or "\x00" in expanded:
            return ANY_FOLDER
        if _GLOB.search(expanded):
            return self._glob_refusal(expanded, cwd)
        return self.holding(cwd / expanded)

    def _glob_refusal(self, pattern: str, cwd: Path) -> int | None:
        """A glob is refused when the directory it starts from holds, or is held by, an
        entry: it could match its way into the folder whatever its tail says."""

        parts = Path(cwd / pattern).parts
        first = next(i for i, part in enumerate(parts) if _GLOB.search(part))
        literal = Path(*parts[:first]) if first else Path(parts[0] if parts else "/")
        for form in _forms(str(literal)):
            for index, folder in enumerate(self._folders):
                if folder.holds(form.parts) or folder.is_under(form.parts):
                    return index
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
    else:
        candidates.extend(word[found.start() :] for found in _PATH_AFTER_SEPARATOR.finditer(word))
    # Over-reading costs nothing: a piece that is no path resolves inside the Workspace,
    # which no entry may hold.
    pieces = [piece for candidate in candidates for piece in _LIST_SEPARATOR.split(candidate)]
    return tuple(dict.fromkeys(c for c in (*candidates, *pieces) if c))


def _plainly_a_path(word: str) -> bool:
    return not word.startswith("-") and "://" not in word and ("/" in word or word in {".", ".."})


def _runs(argv: Sequence[str], words: Sequence[str]) -> bool:
    """Whether ``argv`` runs the command an entry names: its program by name, then its
    arguments word for word."""

    if not argv or not words or len(argv) < len(words):
        return False
    return Path(argv[0]).name == words[0] and list(argv[1 : len(words)]) == list(words[1:])


class DirectiveGuardrails:
    """One Directive's Guardrails, ready for the Runner's checks.

    ``workspace`` is the Directive's own Workspace: the Organisation's folders are paths
    inside it, and an empty folder list allows all of it.
    """

    def __init__(
        self,
        guardrails: Guardrails,
        *,
        workspace: Path,
        home: str | None = None,
        case_insensitive: Callable[[Path], bool] = case_insensitive_filesystem,
    ) -> None:
        self.guardrails = guardrails
        self._workspace = Path(os.path.normpath(workspace))
        self._commands = tuple(entry.split() for entry in guardrails.commands)
        self._removed_commands = tuple(
            (removed.entry.split(), removed)
            for removed in guardrails.removed_from(GuardrailPart.COMMANDS)
        )
        self._removed_folders = guardrails.removed_from(GuardrailPart.FOLDERS)
        # A removed folder may be anywhere (`~/clients`) or inside the Workspace (`secrets`).
        self.denied_folders = DeniedFolders(
            [
                str(self._workspace / expand_home(removed.entry, home))
                for removed in self._removed_folders
            ],
            home=home,
            case_insensitive=case_insensitive,
        )
        self._allowed_folders = tuple(
            Path(os.path.normpath(self._workspace / entry)) for entry in guardrails.folders
        )
        self._removed_network = guardrails.removed_from(GuardrailPart.NETWORK)

    # ------------------------------------------------------------------ commands

    @property
    def lists_commands(self) -> bool:
        """Whether the Organisation listed the commands; if not, the built-in policy holds."""

        return bool(self._commands)

    def removed_command(self, argv: Sequence[str]) -> GuardrailRefusal | None:
        """The narrowing layer that removed what ``argv`` runs, if one did."""

        for words, removed in self._removed_commands:
            if _runs(argv, words):
                return GuardrailRefusal(GuardrailPart.COMMANDS, removed.entry, removed.layer)
        return None

    def listed_command(self, argv: Sequence[str]) -> bool:
        return any(_runs(argv, words) for words in self._commands)

    @staticmethod
    def unlisted_command(argv: Sequence[str]) -> GuardrailRefusal:
        """What a command the Organisation's list does not name is refused as."""

        program = Path(argv[0]).name if argv else ""
        return GuardrailRefusal(GuardrailPart.COMMANDS, program, GuardrailLayer.ORGANISATION)

    # ------------------------------------------------------------------ folders

    def folder_refusal(self, words: Iterable[str], cwd: Path) -> GuardrailRefusal | None:
        """The first folder rule a word breaks: a removed folder, then the allow-list."""

        words = tuple(words)
        index = self.denied_folders.refusal(words, cwd)
        if index is not None:
            return self._denied(index)
        return self._outside_allowed(words, cwd)

    def script_folder_refusal(self, script: str, cwd: Path) -> GuardrailRefusal | None:
        """As :meth:`folder_refusal`, for a shell script the policy cannot parse."""

        words = _SHELL_WORD_BREAK.split(script)
        index = self.denied_folders.refusal((script, *words), cwd)
        if index is not None:
            return self._denied(index)
        return self._outside_allowed(words, cwd)

    def _denied(self, index: int) -> GuardrailRefusal:
        # A word with a variable in it could reach any removed folder; the first one is named.
        removed = self._removed_folders[0 if index == ANY_FOLDER else index]
        return GuardrailRefusal(GuardrailPart.FOLDERS, removed.entry, removed.layer)

    def _outside_allowed(self, words: Iterable[str], cwd: Path) -> GuardrailRefusal | None:
        if not self._allowed_folders:
            return None
        for word in words:
            if not _plainly_a_path(word):
                continue
            path = Path(os.path.normpath(cwd / expand_home(word)))
            if not path.is_relative_to(self._workspace) or path == self._workspace:
                continue
            if not any(path.is_relative_to(allowed) for allowed in self._allowed_folders):
                return GuardrailRefusal(
                    GuardrailPart.FOLDERS,
                    path.relative_to(self._workspace).as_posix(),
                    GuardrailLayer.ORGANISATION,
                )
        return None

    # ------------------------------------------------------------------ network

    @property
    def allowed_network(self) -> tuple[str, ...]:
        return self.guardrails.network

    @property
    def denied_network(self) -> tuple[str, ...]:
        return tuple(removed.entry for removed in self._removed_network)

    def network_refusal(self, host: str, port: int) -> GuardrailRefusal:
        """Who refused a destination the egress proxy turned away."""

        index = destination_denied(self.denied_network, host, port)
        if index is not None:
            removed = self._removed_network[index]
            return GuardrailRefusal(GuardrailPart.NETWORK, removed.entry, removed.layer)
        return GuardrailRefusal(GuardrailPart.NETWORK, host, GuardrailLayer.ORGANISATION)


def turn_timeout(
    settings_seconds: int, guardrails: Guardrails | None
) -> tuple[int, GuardrailRefusal | None]:
    """How long one turn may run, and the refusal to report if it runs that long.

    The Guardrails' wall-clock limit binds only where it is shorter than the Runner's own
    timeout; then an overrun is that layer's refusal, else it is the Runner's own timeout.
    """

    limit = None if guardrails is None else guardrails.limit(GuardrailLimitName.WALL_CLOCK_SECONDS)
    if limit is None or limit.value >= settings_seconds:
        return settings_seconds, None
    return limit.value, GuardrailRefusal(
        GuardrailPart.LIMITS, GuardrailLimitName.WALL_CLOCK_SECONDS.value, limit.layer
    )
