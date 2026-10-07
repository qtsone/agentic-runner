"""flag > env > config file, for every Runner setting (map ticket 26 §2, PRD issue 45).

Buildkite never documented its own order and it took reading the loader to establish it.
Ours is documented on day one, and this is what holds it there: three settings, each
resolved from all three layers at once so a reordering cannot pass by accident.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from agentic_runner.cli import main
from agentic_runner.config import CONFIG_FILE_ENV, RunnerConfig, load

SETTINGS = ("hooks_path", "workspace_root", "state_dir")


def _config_file(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        "\n".join(
            (
                'hooks_path = "/from/file/hooks"',
                'workspace_root = "/from/file/workspaces"',
                'state_dir = "/from/file/state"',
                'log_level = "debug"',
            )
        )
    )
    return path


def test_a_flag_beats_an_env_var_beats_the_config_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for setting in SETTINGS:
        monkeypatch.setenv(f"AGENTIC_RUNNER_{setting.upper()}", f"/from/env/{setting}")

    config = load(
        config_file=_config_file(tmp_path),
        hooks_path=Path("/from/flag/hooks_path"),
        workspace_root=Path("/from/flag/workspace_root"),
        state_dir=Path("/from/flag/state_dir"),
    )

    for setting in SETTINGS:
        assert getattr(config, setting) == Path(f"/from/flag/{setting}")


def test_an_env_var_beats_the_config_file_when_no_flag_is_passed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for setting in SETTINGS:
        monkeypatch.setenv(f"AGENTIC_RUNNER_{setting.upper()}", f"/from/env/{setting}")

    config = load(config_file=_config_file(tmp_path))

    for setting in SETTINGS:
        assert getattr(config, setting) == Path(f"/from/env/{setting}")
    # Untouched by either layer, so the file still decides.
    assert config.log_level == "debug"


def test_the_config_file_decides_when_nothing_else_is_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for setting in SETTINGS:
        monkeypatch.delenv(f"AGENTIC_RUNNER_{setting.upper()}", raising=False)

    config = load(config_file=_config_file(tmp_path))

    assert config.hooks_path == Path("/from/file/hooks")
    assert config.workspace_root == Path("/from/file/workspaces")
    assert config.state_dir == Path("/from/file/state")


def test_an_unset_flag_does_not_overwrite_the_layers_below_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--hooks-path`` not passed is ``None``, which is not a value (argparse's shape)."""

    monkeypatch.setenv("AGENTIC_RUNNER_HOOKS_PATH", "/from/env/hooks")

    config = load(config_file=_config_file(tmp_path), hooks_path=None, state_dir=None)

    assert config.hooks_path == Path("/from/env/hooks")
    assert config.state_dir == Path("/from/file/state")


def test_the_config_file_may_carry_a_key_this_release_does_not_know(tmp_path: Path) -> None:
    """One file, a fleet upgraded one Runner at a time: a newer key must not stop an
    older Runner starting. A mistyped *flag* still fails, because it was typed here."""

    path = tmp_path / "config.toml"
    path.write_text('hooks_path = "/from/file/hooks"\nfuture_setting = "whatever"\n')

    assert load(config_file=path).hooks_path == Path("/from/file/hooks")
    with pytest.raises(ValidationError):
        RunnerConfig(futuer_setting="typo")  # type: ignore[call-arg]


def test_the_config_file_path_is_itself_a_flag_then_an_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    flagged = tmp_path / "flagged.toml"
    flagged.write_text('hooks_path = "/flagged/hooks"\n')
    monkeypatch.setenv(CONFIG_FILE_ENV, str(_config_file(tmp_path)))

    assert load().hooks_path == Path("/from/file/hooks")
    assert load(config_file=flagged).hooks_path == Path("/flagged/hooks")


def test_the_cli_prints_the_resolved_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`agentic-runner config` is how an operator settles "which layer won" on the box."""

    monkeypatch.setenv(CONFIG_FILE_ENV, str(_config_file(tmp_path)))
    monkeypatch.setenv("AGENTIC_RUNNER_STATE_DIR", "/from/env/state")

    assert main(["--hooks-path", "/from/flag/hooks", "config"]) == 0

    printed = capsys.readouterr().out
    assert '"hooks_path": "/from/flag/hooks"' in printed
    assert '"state_dir": "/from/env/state"' in printed
    assert '"workspace_root": "/from/file/workspaces"' in printed
