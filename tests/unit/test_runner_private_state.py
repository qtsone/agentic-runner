"""The state directory is the Runner's alone (runner-repo issue 08).

It holds the identity's private key and the Recipient Key. What this pins:

* a missing state directory is created 0700;
* the Runner refuses -- non-zero, before anything registers -- a symlinked directory, a
  symlinked file, a directory or file another uid owns, a 0755 directory, a 0644 file;
* the identity file is 0600 from its first byte (the temp file it is renamed from), and a
  write interrupted before the rename leaves the previous identity whole.
"""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest

from agentic_runner import service
from agentic_runner.lifecycle import LIFECYCLE_FILENAME
from agentic_runner.private_state import (
    UnsafeStateError,
    ensure_private_dir,
    private_read,
)
from agentic_runner.registration import STATE_FILENAME, RunnerState, load_state, save_state


def _identity(name: str = "identity") -> RunnerState:
    return RunnerState(
        runner_id=uuid4(),
        identity_id=name,
        private_key_pem="-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----\n",
        temporal_namespace="org-test",
        task_queue="runner.test",
    )


def _private_dir(tmp_path: Path) -> Path:
    state = tmp_path / "state"
    ensure_private_dir(state)
    return state


def test_a_missing_state_directory_is_created_0700(tmp_path: Path) -> None:
    state = tmp_path / "parent" / "state"

    ensure_private_dir(state)

    assert state.stat().st_mode & 0o777 == 0o700


def test_a_symlinked_state_directory_is_refused(tmp_path: Path) -> None:
    real = _private_dir(tmp_path)
    link = tmp_path / "link"
    link.symlink_to(real)

    with pytest.raises(UnsafeStateError, match="symlink"):
        ensure_private_dir(link)


def test_a_symlinked_state_file_is_refused(tmp_path: Path) -> None:
    state = _private_dir(tmp_path)
    elsewhere = tmp_path / "planted.json"
    elsewhere.write_text(_identity().model_dump_json())
    (state / STATE_FILENAME).symlink_to(elsewhere)

    with pytest.raises(UnsafeStateError, match="symlink"):
        load_state(state)


def test_a_state_directory_open_to_group_or_others_is_refused(tmp_path: Path) -> None:
    state = _private_dir(tmp_path)
    state.chmod(0o755)

    with pytest.raises(UnsafeStateError, match="0755"):
        ensure_private_dir(state)


def test_a_state_file_open_to_group_or_others_is_refused(tmp_path: Path) -> None:
    state = _private_dir(tmp_path)
    save_state(state, _identity())
    (state / STATE_FILENAME).chmod(0o644)

    with pytest.raises(UnsafeStateError, match="0644"):
        load_state(state)


def test_a_state_directory_or_file_another_uid_owns_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _private_dir(tmp_path)
    save_state(state, _identity())
    # Chowning to another uid needs root; a Runner whose euid is not the owner is the same
    # fact seen from the other side.
    monkeypatch.setattr(os, "geteuid", lambda: os.getuid() + 1)

    with pytest.raises(UnsafeStateError, match="owned by uid"):
        ensure_private_dir(state)
    with pytest.raises(UnsafeStateError, match="owned by uid"):
        private_read(state / STATE_FILENAME)


def test_the_identity_file_is_0600_from_its_first_byte(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _private_dir(tmp_path)
    modes: list[int] = []
    real_replace = os.replace

    def observe(source: str | Path, target: str | Path) -> None:
        modes.append(os.stat(source).st_mode & 0o777)
        real_replace(source, target)

    monkeypatch.setattr(os, "replace", observe)
    # A permissive umask is the case the old write-then-chmod got wrong.
    previous_umask = os.umask(0o000)
    try:
        save_state(state, _identity())
    finally:
        os.umask(previous_umask)

    assert modes == [0o600]
    assert (state / STATE_FILENAME).stat().st_mode & 0o777 == 0o600


def test_an_interrupted_write_leaves_the_previous_identity_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _private_dir(tmp_path)
    previous = _identity("previous")
    save_state(state, previous)

    def crash(source: str | Path, target: str | Path) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", crash)
    with pytest.raises(OSError, match="disk full"):
        save_state(state, _identity("next"))
    monkeypatch.undo()

    assert load_state(state) == previous
    assert sorted(path.name for path in state.iterdir()) == [STATE_FILENAME]


def test_a_write_replaces_a_planted_symlink_rather_than_following_it(tmp_path: Path) -> None:
    state = _private_dir(tmp_path)
    outside = tmp_path / "outside"
    outside.write_bytes(b"untouched")
    (state / STATE_FILENAME).symlink_to(outside)

    save_state(state, _identity())

    assert outside.read_bytes() == b"untouched"
    assert not (state / STATE_FILENAME).is_symlink()


def _environment(monkeypatch: pytest.MonkeyPatch, state_dir: Path) -> None:
    monkeypatch.setenv("AGENTIC_CONTROL_PLANE_URL", "https://control-plane.test")
    monkeypatch.setenv("AGENTIC_AGENT_TOKEN", "agent-token-of-sixteen-plus-chars")
    monkeypatch.setenv("AGENTIC_RUNNER_STATE_DIR", str(state_dir))
    monkeypatch.setenv("AGENTIC_RUNNER_ISOLATION", "none")
    monkeypatch.setenv("TEMPORAL_ADDRESS", "temporal.test:7233")


async def _refused_connect(address: str, **kwargs: object) -> object:
    raise AssertionError("a refused Runner must not reach Temporal")


@pytest.mark.asyncio
async def test_the_runner_refuses_to_start_on_a_state_directory_open_to_others(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    state = _private_dir(tmp_path)
    state.chmod(0o755)
    _environment(monkeypatch, state)

    exit_code = await service.run(can_change_uid=False, connect=_refused_connect)

    assert exit_code == 1
    assert "refusing to start (unsafe_state_dir)" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_the_runner_refuses_to_start_on_a_state_file_open_to_others(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    state = _private_dir(tmp_path)
    save_state(state, _identity())
    (state / STATE_FILENAME).chmod(0o644)
    _environment(monkeypatch, state)

    exit_code = await service.run(can_change_uid=False, connect=_refused_connect)

    assert exit_code == 1
    assert "refusing to start (unsafe_state_dir)" in capsys.readouterr().out
    assert not (state / LIFECYCLE_FILENAME).exists()


@pytest.mark.asyncio
async def test_a_lifecycle_outbox_open_to_others_refuses_before_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The upgrade case: an earlier release left the outbox 0644, and it is first read only
    # after `register()` -- so the start check, not the read, has to catch it.
    state = _private_dir(tmp_path)
    save_state(state, _identity())
    (state / LIFECYCLE_FILENAME).write_text("[]")
    (state / LIFECYCLE_FILENAME).chmod(0o644)
    _environment(monkeypatch, state)

    async def _must_not_register(**kwargs: object) -> object:
        raise AssertionError("a refused Runner must not register")

    monkeypatch.setattr(service, "register", _must_not_register)

    exit_code = await service.run(can_change_uid=False, connect=_refused_connect)

    assert exit_code == 1
    assert "refusing to start (unsafe_state_dir)" in capsys.readouterr().out
