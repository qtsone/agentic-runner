"""One OS uid per Contract on the Runner (ADR-0015 §1-§3, PRD issue 30).

The Runner process keeps its own uid and is the only thing that touches a credential
value. Everything that executes repository code or runs beside an Agent — the Directive
subprocess, the verifier, later Runner Hooks and stdio MCP servers — is spawned as the
Contract's own unprivileged uid, inside a Contract directory no other Contract's uid can
read.

Layout under ``WORKSPACE_ROOT`` (the whole of what a termination wipe deletes)::

    {contract_id}/                          0700, the Contract's uid
    {contract_id}/{work_record_id}          the Workspace (ADR-0015 §2)
    {contract_id}/harness/{runtime_kind}    CODEX_HOME / CLAUDE_CONFIG_DIR (ADR-0015 §4)
    {contract_id}/tmp                       TMPDIR (ADR-0015 §4)

Everything here lives in the worker tree so M3 (issue 36) moves it into ``agentic-runner``
unchanged: it imports nothing from the platform's services or database.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import resource
import shutil
import stat
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final
from uuid import UUID

from agentic_runner.integrations.git.workspace import contract_workspace_path
from agentic_runner.private_state import ensure_private_dir, private_read, private_write

__all__ = [
    "NO_CONTRACT",
    "UID_MAP_FILENAME",
    "ContractIsolation",
    "ContractIsolationError",
    "ContractResidue",
    "DirectiveSandbox",
    "contract_path_segment",
]

# A Work Record with no Contract bound — every row predating issue 08's backfill, and any
# flow that binds none. It gets its own directory rather than a share of somebody else's,
# but no uid: there is no Contract to be the OS line, and refusing the run would stop the
# platform that is deploying the Contract layer (the same posture grant enforcement takes
# for an Agent-less Work Record).
NO_CONTRACT: Final[str] = "no-contract"

_logger = logging.getLogger(__name__)

_HARNESS_DIR: Final[str] = "harness"
_TMP_DIR: Final[str] = "tmp"
# Everything directly under a Contract's directory that is not one of its Workspaces.
_RESERVED_DIRS: Final[frozenset[str]] = frozenset({_HARNESS_DIR, _TMP_DIR})
# The Runner's own git metadata inside a checkout. Never handed to the Contract: see
# `hand_workspace_to_contract`.
_GIT_DIR: Final[str] = ".git"
# Read by the Contract's git, not the Runner's (whose HOME is the credential-bearing
# `.agentic-os-git-home`), so it only ever relaxes a check for the Contract's own uid.
_CONTRACT_GITCONFIG: Final[str] = ".gitconfig"
_CONTRACT_GITCONFIG_BODY: Final[str] = "[safe]\n\tdirectory = *\n"
UID_MAP_FILENAME: Final[str] = "contract-uids.json"
_UID_LOCK_FILE: Final[str] = "contract-uids.lock"
# Path segments are platform ids (map ticket 07), i.e. UUIDs — plus the sentinel above.
_PATH_SEGMENT_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_RUNTIME_KIND_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9_]{0,31}$")

_DIR_MODE: Final[int] = 0o700


class ContractIsolationError(RuntimeError):
    """Raised when a Contract's uid or directory cannot be established."""


def _is_uuid(name: str) -> bool:
    try:
        UUID(name)
    except ValueError:
        return False
    return True


def _deny_group_and_other_writes(git_dir: Path) -> None:
    """Root ownership of ``.git`` refuses nothing while its entries are world-writable.

    git creates every ``.git`` entry at ``0777 & ~umask``, so the Runner's umask decides
    whether the Contract can plant a ``pre-commit`` hook. The ARC runner container runs
    jobs with umask 000, which is how ``test_contract_uid_isolation`` first caught it;
    a Runner process started the same way in production would have the same hole.
    """

    if git_dir.is_symlink():
        return
    entries: list[Path] = [git_dir] if git_dir.exists() else []
    if git_dir.is_dir():
        for directory, _dirnames, filenames in os.walk(git_dir):
            entries.extend(Path(directory) / filename for filename in filenames)
            entries.extend(Path(directory) / name for name in _dirnames)
    for entry in entries:
        if entry.is_symlink():
            continue
        mode = stat.S_IMODE(entry.lstat().st_mode)
        if mode & 0o022:
            entry.chmod(mode & ~0o022)


def contract_path_segment(contract_id: str | None) -> str:
    """The directory name a Contract's tree lives under, validated as a path segment."""

    segment = (contract_id or "").strip() or NO_CONTRACT
    if not _PATH_SEGMENT_RE.fullmatch(segment):
        raise ContractIsolationError(f"contract id is not a safe path segment: {contract_id!r}")
    return segment


@dataclass(frozen=True, slots=True)
class DirectiveSandbox:
    """Where a Contract's subprocess lives and what it may not exceed (ADR-0015 §1).

    ``uid`` is None on a Runner that cannot separate uids (a workstation, or a container
    without ``CAP_SETUID``): the directories are still 0700 and per Contract, but the
    process runs as the Runner's own user. ADR-0015 §5 turns that into a declared,
    routing-visible single-Contract mode in M3; M1 only has to not pretend otherwise.

    ``max_memory_bytes`` is None where the kernel cannot hold a memory ceiling (macOS, see
    ``ContractIsolation``): the spawn runs without one rather than not at all.
    """

    home_dir: Path
    harness_config_dir: Path
    max_processes: int
    max_memory_bytes: int | None
    uid: int | None = None
    gid: int | None = None

    @property
    def tmp_dir(self) -> Path:
        """This Contract's ``TMPDIR``.

        The pod's ``/tmp`` is one emptyDir every Contract uid can write and list, so a
        harness CLI's temp files would leak across the line the per-Contract harness root
        draws. Inside the Contract's own 0700 tree they do not.
        """

        return self.home_dir / _TMP_DIR

    def spawn_kwargs(self) -> dict[str, Any]:
        """What ``subprocess`` needs to put one spawn under this Contract's floor.

        The uid/gid drop is handed to ``subprocess``'s own fork-exec path (``user`` /
        ``group`` / ``extra_groups``), which does it in C: ``preexec_fn`` runs Python
        between fork and exec in a Temporal worker that has real threads, which CPython
        documents as unsafe, so only what has no C equivalent — the two rlimits — is left
        in the hook.
        """

        kwargs: dict[str, Any] = {"preexec_fn": self.preexec()}
        if self.gid is not None:
            # Ordered by subprocess itself: setgroups, then setgid, then setuid.
            kwargs["extra_groups"] = []
            kwargs["group"] = self.gid
        if self.uid is not None:
            kwargs["user"] = self.uid
        return kwargs

    def preexec(self) -> Callable[[], None]:
        """The between-fork-and-exec hook that applies the rlimit floor (ADR-0011 §12).

        Not attenuable: it is applied by the Runner to every spawn, after the command
        policy has already accepted the argv, and no Grant reaches it. It runs *after*
        the uid drop `spawn_kwargs` asks subprocess for, so both limits land on the
        Contract's uid; lowering a soft and hard limit needs no privilege.

        The memory ceiling is ``RLIMIT_DATA``, not ``RLIMIT_AS``: both harness CLIs run on
        node, and V8 *reserves* a multi-gigabyte pointer-compression cage of PROT_NONE
        address space at start-up. An ``RLIMIT_AS`` of a few GiB kills node before it runs
        a single turn, while ``RLIMIT_DATA`` counts only writable private mappings — the
        heap that actually grows — so the ceiling bites on the runaway and not on start-up.
        """

        max_processes = self.max_processes
        max_memory_bytes = self.max_memory_bytes

        def apply_floor() -> None:
            resource.setrlimit(resource.RLIMIT_NPROC, (max_processes, max_processes))
            if max_memory_bytes is not None:
                resource.setrlimit(resource.RLIMIT_DATA, (max_memory_bytes, max_memory_bytes))

        return apply_floor


@dataclass(frozen=True, slots=True)
class ContractResidue:
    """What a termination wipe removed — ids and counts only (ADR-0015 §2, issue 30)."""

    contract_id: str
    workspaces_removed: int
    harness_roots_removed: int
    uid_retired: bool


class ContractIsolation:
    """Allocates one uid per Contract and owns that Contract's directory tree.

    The uid map is persisted in the Runner's own state directory so a restart reuses the
    uid a Contract's files are already owned by. No passwd entry is created: nothing here
    needs name resolution, and a Runner has no business editing ``/etc/passwd``.
    """

    def __init__(
        self,
        *,
        workspace_root: Path,
        state_dir: Path,
        uid_min: int,
        uid_max: int,
        max_processes: int,
        memory_limit_bytes: int,
        can_separate_uids: bool | None = None,
        can_limit_memory: bool | None = None,
    ) -> None:
        if uid_min <= 0 or uid_max < uid_min:
            raise ValueError("contract uid range must be a positive, non-empty range")
        self._workspace_root = workspace_root.resolve(strict=False)
        self._state_dir = state_dir.resolve(strict=False)
        self._uid_min = uid_min
        self._uid_max = uid_max
        self._max_processes = max_processes
        self._memory_limit_bytes = memory_limit_bytes
        # Detected, not declared: ADR-0015 §5's `isolation:` setting and the routing rule
        # that makes a `none` Runner single-Contract are M3 (issue 42). Until then the
        # honest answer is whether this process can actually change uid.
        self._can_separate_uids = (
            os.geteuid() == 0 if can_separate_uids is None else can_separate_uids
        )
        # XNU refuses an RLIMIT_DATA below the address space a process already maps, and
        # every arm64 macOS process maps hundreds of GiB of shared cache and reservations
        # before it runs a line — so any ceiling worth setting fails `preexec_fn` and the
        # spawn with it (QTS-1319). macOS enforces neither RLIMIT_AS nor RLIMIT_RSS, so
        # there is no rlimit to fall back to: the floor there is RLIMIT_NPROC alone.
        self._can_limit_memory = (
            sys.platform != "darwin" if can_limit_memory is None else can_limit_memory
        )
        if not self._can_limit_memory:
            _logger.warning(
                "this host refuses RLIMIT_DATA: Directive spawns run without the %d-byte "
                "memory ceiling",
                memory_limit_bytes,
            )

    @property
    def can_limit_memory(self) -> bool:
        return self._can_limit_memory

    @property
    def can_separate_uids(self) -> bool:
        return self._can_separate_uids

    def contract_dir(self, contract_id: str | None) -> Path:
        return self._workspace_root / contract_path_segment(contract_id)

    def workspace_path(self, contract_id: str | None, work_record_id: str) -> Path:
        """``{contract_id}/{work_record_id}`` — one Workspace per Work Record (17 A5)."""

        return contract_workspace_path(
            workspace_root=self._workspace_root,
            contract_id=contract_path_segment(contract_id),
            work_record_id=work_record_id,
        )

    def harness_config_dir(self, contract_id: str | None, runtime_kind: str) -> Path:
        if not _RUNTIME_KIND_RE.fullmatch(runtime_kind):
            raise ContractIsolationError(f"runtime kind is not a safe segment: {runtime_kind!r}")
        return self.contract_dir(contract_id) / _HARNESS_DIR / runtime_kind

    def has_uid(self, contract_id: str | None) -> bool:
        """Whether this Contract already holds a uid.

        Read before ``uid_for`` by the caller that records the allocation as an Evidence
        Event, so only the call that actually allocates writes one (PRD issue 30).
        """

        segment = contract_path_segment(contract_id)
        if not self._can_separate_uids or segment == NO_CONTRACT:
            return False
        return segment in self._read_uid_map()

    def uid_for(self, contract_id: str | None) -> int | None:
        """This Contract's uid, allocated from the Runner-local range on first sight."""

        segment = contract_path_segment(contract_id)
        if not self._can_separate_uids or segment == NO_CONTRACT:
            return None
        with self._uid_map_locked():
            allocated = self._read_uid_map()
            existing = allocated.get(segment)
            if existing is not None:
                return existing
            taken = set(allocated.values())
            for candidate in range(self._uid_min, self._uid_max + 1):
                if candidate not in taken:
                    allocated[segment] = candidate
                    self._write_uid_map(allocated)
                    return candidate
        raise ContractIsolationError(
            f"contract uid range {self._uid_min}-{self._uid_max} is exhausted"
        )

    def sandbox(
        self,
        contract_id: str | None,
        *,
        runtime_kind: str,
        memory_limit_bytes: int | None = None,
    ) -> DirectiveSandbox:
        """Prepare the Contract's harness root and describe the floor its spawns run under."""

        uid = self.uid_for(contract_id)
        home = self._ensure_dir(self.contract_dir(contract_id), uid)
        harness = self._ensure_dir(self.harness_config_dir(contract_id, runtime_kind), uid)
        self._ensure_dir(home / _TMP_DIR, uid)
        self._write_contract_gitconfig(home, uid)
        return DirectiveSandbox(
            home_dir=home,
            harness_config_dir=harness,
            max_processes=self._max_processes,
            max_memory_bytes=(
                (memory_limit_bytes or self._memory_limit_bytes) if self._can_limit_memory else None
            ),
            uid=uid,
            gid=uid,
        )

    def existing_sandbox(
        self, contract_id: str | None, *, runtime_kind: str
    ) -> DirectiveSandbox | None:
        """``sandbox`` only for a harness root already on disk, else ``None``.

        For a caller that must never be the one to create a Contract's tree or allocate
        its uid -- a terminated Contract's wipe may have just retired both. Synchronous
        on purpose: no await may fall between the check and ``sandbox``, or a wipe on the
        same event loop could land in between.
        """

        if not self.harness_config_dir(contract_id, runtime_kind).is_dir():
            return None
        if self._can_separate_uids and not self.has_uid(contract_id):
            return None
        return self.sandbox(contract_id, runtime_kind=runtime_kind)

    def prepare_workspace(self, contract_id: str | None, work_record_id: str) -> Path:
        """Create ``{contract_id}/{work_record_id}`` 0700, owned by the Contract's uid."""

        uid = self.uid_for(contract_id)
        self._ensure_dir(self.contract_dir(contract_id), uid)
        return self._ensure_dir(self.workspace_path(contract_id, work_record_id), uid)

    def hand_workspace_to_contract(self, contract_id: str | None, workspace_path: Path) -> None:
        """Re-own the checkout before a Directive runs in it — everything but ``.git``.

        The Runner clones, fetches, commits and pushes as its own uid (it is the only
        thing that may touch the git credential), so every git write lands root-owned in
        a Contract-owned tree. The Directive that runs next is the Contract's uid and has
        to be able to write what git just wrote.

        ``.git`` is deliberately left out of that hand-over. It is the one part of a
        checkout the Runner's own git reads as *instructions* rather than as data: a
        Contract that could write it would put a ``pre-commit`` hook in ``.git/hooks/``,
        or ``core.fsmonitor`` / ``core.hooksPath`` / ``filter.*.clean`` in
        ``.git/config``, and the Runner's next ``git status`` / ``git add`` /
        ``git commit`` would run that command as the Runner — with CAP_SETUID, CAP_CHOWN
        and CAP_DAC_OVERRIDE, i.e. with every other Contract's tree, the git credential
        and the uid map. Naming the settings in ``-c`` flags does not close it
        (``filter.*`` and ``diff.*.textconv`` are driven by a committed
        ``.gitattributes`` and are not enumerable), so the directory itself stays the
        Runner's. Left root-owned it is still readable, so the Directive's own
        ``git status`` / ``git diff`` / ``git log`` work; only writing is refused, and the
        Runner already owns the commit.

        Ownership of ``.git`` is **not** on its own the boundary, and this function does
        not claim to be one. The Contract owns the directory that *contains* ``.git``,
        and on POSIX renaming or creating an entry is governed by write+execute on the
        parent — so a Directive can move the Runner's ``.git`` aside and drop a replica
        of its own in place. What closes it is
        ``integrations.git.workspace.require_runner_owned_git_dir``, re-checking that
        ``.git`` is still a Runner-owned directory before every Runner git call into a
        Workspace. Keeping the chown off ``.git`` is what makes that check cheap and
        never false-positive; the check is what makes it hold.

        ponytail: a full-tree chown before each Directive. Cheap next to a clone, but it
        is O(files) per Directive — revisit with a shared supplementary group if a large
        monorepo makes it show up.
        """

        uid = self.uid_for(contract_id)
        if uid is None:
            return
        resolved = workspace_path.resolve(strict=False)
        if self._workspace_root not in resolved.parents:
            raise ContractIsolationError("workspace_path must be under the workspace root")
        os.chown(resolved, uid, uid)
        for directory, dirnames, filenames in os.walk(resolved):
            dirnames[:] = [name for name in dirnames if name != _GIT_DIR]
            os.chown(directory, uid, uid)
            for filename in filenames:
                entry = Path(directory) / filename
                if not entry.is_symlink():
                    os.chown(entry, uid, uid)
        _deny_group_and_other_writes(resolved / _GIT_DIR)

    def wipe(self, contract_id: str | None) -> ContractResidue:
        """Delete the Contract's tree and retire its uid (17 A6). Idempotent."""

        segment = contract_path_segment(contract_id)
        contract_dir = self.contract_dir(segment)
        workspaces = 0
        harness_roots = 0
        harness_dir = contract_dir / _HARNESS_DIR
        if contract_dir.is_dir():
            if harness_dir.is_dir():
                harness_roots = sum(1 for entry in harness_dir.iterdir() if entry.is_dir())
            workspaces = sum(
                1
                for entry in contract_dir.iterdir()
                if entry.is_dir() and entry.name not in _RESERVED_DIRS
            )
            shutil.rmtree(contract_dir, ignore_errors=True)
            if contract_dir.exists():
                # A partial wipe is not a wipe. Report nothing removed and keep the uid:
                # a terminated Contract's Evidence must not claim a tree that is still on
                # disk, and the files left behind are still owned by that uid, so handing
                # it to the next Contract would hand over their contents with it.
                return ContractResidue(
                    contract_id=segment,
                    workspaces_removed=0,
                    harness_roots_removed=0,
                    uid_retired=False,
                )
        with self._uid_map_locked():
            allocated = self._read_uid_map()
            uid_retired = allocated.pop(segment, None) is not None
            if uid_retired:
                self._write_uid_map(allocated)
        return ContractResidue(
            contract_id=segment,
            workspaces_removed=workspaces,
            harness_roots_removed=harness_roots,
            uid_retired=uid_retired,
        )

    def held_work_record_ids(self) -> list[tuple[str, str]]:
        """Every ``(contract_id, work_record_id)`` Workspace currently on this Runner.

        Only directories that are really Work Record ids. Both callers send the names on
        to the control plane as ``UUID``s, and one stray directory under one Contract
        would otherwise 422 the whole daily retention sweep — every other Contract's
        expired Workspaces with it — rather than just being skipped.
        """

        if not self._workspace_root.is_dir():
            return []
        held: list[tuple[str, str]] = []
        for contract_dir in sorted(self._workspace_root.iterdir()):
            if not contract_dir.is_dir() or not _PATH_SEGMENT_RE.fullmatch(contract_dir.name):
                continue
            for entry in sorted(contract_dir.iterdir()):
                if entry.is_dir() and entry.name not in _RESERVED_DIRS and _is_uuid(entry.name):
                    held.append((contract_dir.name, entry.name))
        return held

    def harness_roots(self) -> list[tuple[str, str]]:
        """Every ``(contract_id, runtime_kind)`` harness root already on this Runner."""

        if not self._workspace_root.is_dir():
            return []
        roots: list[tuple[str, str]] = []
        for contract_dir in sorted(self._workspace_root.iterdir()):
            harness_dir = contract_dir / _HARNESS_DIR
            if not _is_uuid(contract_dir.name) or not harness_dir.is_dir():
                continue
            for entry in sorted(harness_dir.iterdir()):
                if entry.is_dir() and _RUNTIME_KIND_RE.fullmatch(entry.name):
                    roots.append((contract_dir.name, entry.name))
        return roots

    def remove_workspace(self, contract_id: str | None, work_record_id: str) -> bool:
        """Delete one Work Record's Workspace, leaving the Contract's tree standing."""

        path = self.workspace_path(contract_id, work_record_id)
        if not path.is_dir():
            return False
        shutil.rmtree(path, ignore_errors=True)
        return not path.exists()

    def _ensure_dir(self, path: Path, uid: int | None) -> Path:
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(_DIR_MODE)
        if uid is not None:
            os.chown(path, uid, uid)
        return path

    def _write_contract_gitconfig(self, home: Path, uid: int | None) -> None:
        """Let the Contract's own git read the checkout it does not own.

        ``.git`` stays the Runner's (see `hand_workspace_to_contract`), and git refuses a
        repository whose gitdir another user owns. Without this the Directive's own
        ``git status`` / ``git diff`` fails on "dubious ownership". It relaxes nothing:
        the config is read only by git running as the Contract's uid, which gains no
        access it did not already have.
        """

        config = home / _CONTRACT_GITCONFIG
        if config.is_file() and config.read_text(encoding="utf-8") == _CONTRACT_GITCONFIG_BODY:
            return
        config.write_text(_CONTRACT_GITCONFIG_BODY, encoding="utf-8")
        config.chmod(0o600)
        if uid is not None:
            os.chown(config, uid, uid)

    @contextmanager
    def _uid_map_locked(self) -> Iterator[None]:
        """Serialise the uid map's read-modify-write across processes.

        Within one worker the allocation is already atomic (no ``await`` in ``uid_for``),
        but two Runner replicas sharing the state volume would otherwise hand two
        Contracts the same uid — and one uid is the whole isolation boundary.
        """

        ensure_private_dir(self._state_dir)
        descriptor = os.open(
            self._state_dir / _UID_LOCK_FILE, os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(descriptor, "w") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield

    def _uid_map_path(self) -> Path:
        return self._state_dir / UID_MAP_FILENAME

    def _read_uid_map(self) -> dict[str, int]:
        path = self._uid_map_path()
        raw = private_read(path)
        if raw is None:
            return {}
        loaded = json.loads(raw)
        if not isinstance(loaded, dict):
            raise ContractIsolationError(f"contract uid map at {path} is not an object")
        return {str(key): int(value) for key, value in loaded.items()}

    def _write_uid_map(self, allocated: dict[str, int]) -> None:
        # Atomic: a crash mid-write must not leave a half-parsed map that would hand a
        # second Contract a uid another Contract's files are already owned by.
        private_write(self._uid_map_path(), json.dumps(allocated, sort_keys=True).encode("utf-8"))
