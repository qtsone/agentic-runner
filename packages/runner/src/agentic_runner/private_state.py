"""The state directory's one writer and one reader (runner-repo issue 08).

The state directory holds the Runner's private identity key and the Recipient Key, so
nothing in it may be readable by another uid for even an instant, and no crash may leave a
half-written file where the identity was. Every file the Runner writes there goes through
:func:`private_write` and every file it reads through :func:`private_read`; both refuse a
directory or file that is a symlink, belongs to another uid or carries a group or other
permission bit, rather than tightening it -- a state directory in that shape was put there
by someone else, and the Runner cannot know what they read while it was open.
"""

from __future__ import annotations

import contextlib
import errno
import os
import secrets
import stat
from collections.abc import Callable
from pathlib import Path

__all__ = [
    "UnsafeStateError",
    "ensure_private_dir",
    "private_read",
    "private_write",
]

_DIR_MODE = 0o700
_FILE_MODE = 0o600
_GROUP_OTHER_BITS = 0o077


class UnsafeStateError(RuntimeError):
    """The state directory, or a file in it, is not this Runner's alone."""

    reason = "unsafe_state_dir"


def ensure_private_dir(path: Path) -> None:
    """Create ``path`` 0700 if it is missing, else refuse it unless it is private already."""

    try:
        os.mkdir(path, _DIR_MODE)
    except FileExistsError:
        pass
    except FileNotFoundError:
        path.parent.mkdir(parents=True, exist_ok=True)
        os.mkdir(path, _DIR_MODE)
    _require_private(path, os.lstat(path), stat.S_ISDIR, "directory", "chmod 700")


def private_write(path: Path, data: bytes) -> None:
    """Replace ``path`` with ``data``: 0600 from the first byte, and all-or-nothing.

    A temp file beside the target, created exclusively and without following a planted
    symlink, then renamed over it: a reader sees the old content or the new, never a
    truncated identity, and the rename is made durable by syncing the directory too.
    """

    ensure_private_dir(path.parent)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    descriptor = os.open(
        temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, _FILE_MODE
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)
        raise
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def private_read(path: Path) -> bytes | None:
    """``path``'s content, or ``None`` if it does not exist; refused unless private."""

    parent = path.parent
    try:
        parent_status = os.lstat(parent)
    except FileNotFoundError:
        return None
    _require_private(parent, parent_status, stat.S_ISDIR, "directory", "chmod 700")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise UnsafeStateError(f"{path} is a symlink; refusing to read through it") from error
        raise
    with os.fdopen(descriptor, "rb") as handle:
        _require_private(path, os.fstat(handle.fileno()), stat.S_ISREG, "file", "chmod 600")
        return handle.read()


def _require_private(
    path: Path,
    status: os.stat_result,
    is_kind: Callable[[int], bool],
    kind: str,
    remedy: str,
) -> None:
    if stat.S_ISLNK(status.st_mode):
        raise UnsafeStateError(f"{path} is a symlink; the state {kind} must be a real {kind}")
    if not is_kind(status.st_mode):
        raise UnsafeStateError(f"{path} is not a {kind}")
    euid = os.geteuid()
    if status.st_uid != euid:
        raise UnsafeStateError(
            f"{path} is owned by uid {status.st_uid}, not this Runner's uid {euid}"
        )
    if status.st_mode & _GROUP_OTHER_BITS:
        raise UnsafeStateError(
            f"{path} is mode {stat.S_IMODE(status.st_mode):04o}, open to group or others; "
            f"`{remedy} {path}` if nobody else could have read it"
        )
