"""The workstation's Credential Reference store (PRD issue 47, map ticket 23 item 5).

The OS credential store when it is reachable -- Keychain on macOS, Secret Service on
Linux, Credential Manager on Windows -- and a directory of ``0600`` files under the
state directory otherwise. Reachable is the operative word: a login agent reaches the
login keychain and a daemon does not (Apple TN3137), which is half the reason the
workstation Runner is a login agent at all.

Every backend is driven through the OS's own tool or API rather than a Python keyring
library: ``security`` and ``secret-tool`` ship with the OS (or its desktop), and the
Runner stays inside the dependency set an Organisation already audited. Values reach the
tools on **stdin**, never argv, because argv is readable by every process on the box.

The store holds a value per Credential Reference name, scoped by Organisation -- one
process per Organisation (23 item 3) means one service name per process. The Runner
itself only ever *reads* (``credentials.HostCredentialStore``); ``put`` is the host
operator's act, through ``agentic-runner credential set``.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol

from agentic_runner.credentials import DirectoryCredentialStore
from agentic_runner.private_state import private_read, private_write
from agentic_runner_contracts.runner_registration import StoreKind

__all__ = [
    "FileCredentialStore",
    "KeychainStore",
    "SecretServiceStore",
    "WindowsCredentialStore",
    "WorkstationStore",
    "open_workstation_store",
]

Runner = Callable[..., subprocess.CompletedProcess[str]]


class WorkstationStore(Protocol):
    kind: StoreKind

    def get(self, reference: str) -> str | None: ...
    def put(self, reference: str, value: str) -> None: ...


def _single_segment(reference: str) -> str:
    # The same rule `DirectoryCredentialStore` enforces, so a reference that works in one
    # store works in every other.
    if not reference or "/" in reference or "\\" in reference or reference in {".", ".."}:
        raise ValueError(f"Credential Reference {reference!r} is not a single name")
    return reference


class FileCredentialStore(DirectoryCredentialStore):
    """The fallback: one ``0600`` file per reference, in a ``0700`` directory."""

    kind = StoreKind.FILE

    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.root = root

    def get(self, reference: str) -> str | None:
        raw = private_read(self.root / _single_segment(reference))
        return None if raw is None else raw.decode("utf-8").strip()

    def put(self, reference: str, value: str) -> None:
        private_write(self.root / _single_segment(reference), value.encode("utf-8"))


class KeychainStore:
    """macOS: generic passwords in the login keychain, via ``security``."""

    kind = StoreKind.KEYCHAIN

    def __init__(
        self, service: str, *, keychain: Path | None = None, run: Runner = subprocess.run
    ) -> None:
        self._service = service
        self._keychain = [str(keychain)] if keychain is not None else []
        self._run = run

    def available(self) -> bool:
        if shutil.which("security") is None:
            return False
        probe = self._run(
            ["security", "show-keychain-info", *self._keychain],
            capture_output=True,
            text=True,
            check=False,
        )
        return probe.returncode == 0

    def get(self, reference: str) -> str | None:
        found = self._run(
            [
                "security",
                "find-generic-password",
                "-s",
                self._service,
                "-a",
                _single_segment(reference),
                "-w",
                *self._keychain,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        return found.stdout.rstrip("\n") if found.returncode == 0 else None

    def put(self, reference: str, value: str) -> None:
        if "\n" in value:
            raise ValueError("a Keychain value is a single line")
        # `security -i` reads its command from stdin, so the value never enters argv.
        command = " ".join(
            [
                "add-generic-password -U",
                f"-s {_quote(self._service)}",
                f"-a {_quote(_single_segment(reference))}",
                f"-w {_quote(value)}",
                *(_quote(item) for item in self._keychain),
            ]
        )
        stored = self._run(
            ["security", "-i"], input=command + "\n", capture_output=True, text=True, check=False
        )
        if stored.returncode != 0 or stored.stderr.strip():
            raise RuntimeError(f"Keychain refused the value: {stored.stderr.strip()}")


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


class SecretServiceStore:
    """Linux: the desktop's Secret Service (GNOME Keyring, KWallet), via ``secret-tool``."""

    kind = StoreKind.SECRET_SERVICE

    def __init__(self, service: str, *, run: Runner = subprocess.run) -> None:
        self._service = service
        self._run = run

    def available(self) -> bool:
        if shutil.which("secret-tool") is None:
            return False
        # A lookup that finds nothing exits 1 with nothing on stderr; one that cannot
        # reach the bus (a headless box, a daemon) says so on stderr.
        probe = self._run(
            ["secret-tool", "lookup", "service", self._service, "account", "-probe-"],
            capture_output=True,
            text=True,
            check=False,
        )
        return probe.returncode == 0 or not probe.stderr.strip()

    def get(self, reference: str) -> str | None:
        found = self._run(
            ["secret-tool", "lookup", *self._attributes(reference)],
            capture_output=True,
            text=True,
            check=False,
        )
        return found.stdout if found.returncode == 0 and found.stdout else None

    def put(self, reference: str, value: str) -> None:
        stored = self._run(
            [
                "secret-tool",
                "store",
                f"--label=agentic-runner {self._service} {reference}",
                *self._attributes(reference),
            ],
            input=value,
            capture_output=True,
            text=True,
            check=False,
        )
        if stored.returncode != 0:
            raise RuntimeError(f"Secret Service refused the value: {stored.stderr.strip()}")

    def _attributes(self, reference: str) -> Sequence[str]:
        return ["service", self._service, "account", _single_segment(reference)]


class WindowsCredentialStore:
    """Windows: generic credentials in the user's Credential Manager (``CredReadW``)."""

    kind = StoreKind.CREDENTIAL_MANAGER

    def __init__(self, service: str) -> None:
        self._service = service

    def available(self) -> bool:
        return sys.platform == "win32"

    def _target(self, reference: str) -> str:
        return f"{self._service}/{_single_segment(reference)}"

    def get(self, reference: str) -> str | None:
        if sys.platform != "win32":
            return None
        return _win_read(self._target(reference))

    def put(self, reference: str, value: str) -> None:
        if sys.platform != "win32":
            raise RuntimeError("Credential Manager exists only on Windows")
        _win_write(self._target(reference), value)


if sys.platform == "win32":  # pragma: no cover - exercised on Windows only
    import ctypes
    from ctypes import wintypes

    _CRED_TYPE_GENERIC = 1
    _CRED_PERSIST_LOCAL_MACHINE = 2

    class _Credential(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_char)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    _advapi = ctypes.WinDLL("advapi32", use_last_error=True)

    def _win_read(target: str) -> str | None:
        pointer = ctypes.POINTER(_Credential)()
        if not _advapi.CredReadW(target, _CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)):
            return None
        try:
            credential = pointer.contents
            blob = ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize)
            return blob.decode("utf-8")
        finally:
            _advapi.CredFree(pointer)

    def _win_write(target: str, value: str) -> None:
        blob = value.encode("utf-8")
        credential = _Credential()
        credential.Type = _CRED_TYPE_GENERIC
        credential.TargetName = target
        credential.CredentialBlobSize = len(blob)
        credential.CredentialBlob = ctypes.cast(
            ctypes.create_string_buffer(blob, len(blob)), ctypes.POINTER(ctypes.c_char)
        )
        credential.Persist = _CRED_PERSIST_LOCAL_MACHINE
        credential.UserName = "agentic-runner"
        if not _advapi.CredWriteW(ctypes.byref(credential), 0):
            raise ctypes.WinError(ctypes.get_last_error())

else:

    def _win_read(target: str) -> str | None:
        return None

    def _win_write(target: str, value: str) -> None:
        raise RuntimeError("Credential Manager exists only on Windows")


def open_workstation_store(
    org: str, *, fallback: Path, platform: str = sys.platform, run: Runner = subprocess.run
) -> WorkstationStore:
    """The OS store for this platform if it answers, else the ``0600`` file fallback."""

    service = f"agentic-runner.{org}"
    candidates: dict[str, KeychainStore | SecretServiceStore | WindowsCredentialStore] = {
        "darwin": KeychainStore(service, run=run),
        "linux": SecretServiceStore(service, run=run),
        "win32": WindowsCredentialStore(service),
    }
    candidate = candidates.get(platform)
    if candidate is not None and candidate.available():
        return candidate
    return FileCredentialStore(fallback)
