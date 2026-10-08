from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agentic_runner.runtime.verifier_command import CommandValidationError, VerificationResult, run


def fake_runner(
    argv: tuple[str, ...],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout_seconds: float,
) -> subprocess.CompletedProcess[str]:
    del cwd, env, timeout_seconds
    return subprocess.CompletedProcess(
        args=list(argv),
        returncode=0,
        stdout="ok\n",
        stderr="",
    )


def test_forbidden_command_binary_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(CommandValidationError, match="not allowed"):
        run(
            ("git", "status"),
            working_directory=tmp_path,
            workspace_root=tmp_path,
            runner=fake_runner,
        )


@pytest.mark.parametrize(
    "argv",
    (
        ("python -m pytest --version",),
        ("python", "-m", "pytest", "--version", "&&", "id"),
        ("pnpm", "build;cat", "/etc/passwd"),
    ),
)
def test_shell_metacharacters_and_compound_shell_strings_are_rejected(
    tmp_path: Path,
    argv: tuple[str, ...],
) -> None:
    with pytest.raises(CommandValidationError, match="shell"):
        run(argv, working_directory=tmp_path, workspace_root=tmp_path, runner=fake_runner)


def test_path_traversal_working_directory_is_rejected(tmp_path: Path) -> None:
    safe_root = tmp_path / "workspace"
    safe_root.mkdir()

    with pytest.raises(CommandValidationError, match="working directory"):
        run(
            ("npm", "--version"),
            working_directory=safe_root / "..",
            workspace_root=safe_root,
            runner=fake_runner,
        )


@pytest.mark.parametrize(
    "argv",
    (
        ("python", "-m", "pytest", ".."),
        ("python", "-m", "pytest", "--rootdir=.."),
        ("python", "-m", "pytest", "."),
        ("pnpm", "install", "--dir", ".."),
        ("pnpm", "test", "-C", ".."),
        ("pnpm", "test", "--dir=.."),
    ),
)
def test_path_and_cwd_changing_command_arguments_are_rejected(
    tmp_path: Path,
    argv: tuple[str, ...],
) -> None:
    with pytest.raises(CommandValidationError, match="path|cwd|directory|workspace"):
        run(argv, working_directory=tmp_path, workspace_root=tmp_path, runner=fake_runner)


def test_allowed_command_succeeds_and_runner_receives_argv_without_shell(
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}

    def capturing_runner(
        argv: tuple[str, ...],
        *,
        cwd: Path,
        env: dict[str, str],
        timeout_seconds: float,
    ) -> subprocess.CompletedProcess[str]:
        captured["argv"] = argv
        captured["cwd"] = cwd
        captured["env"] = env
        captured["timeout_seconds"] = timeout_seconds
        return subprocess.CompletedProcess(
            args=list(argv), returncode=0, stdout="pytest 8.0\n", stderr=""
        )

    result = run(
        ("python", "-m", "pytest", "--version"),
        working_directory=tmp_path,
        workspace_root=tmp_path,
        runner=capturing_runner,
    )

    assert result.passed is True
    assert captured["argv"] == ("python", "-m", "pytest", "--version")
    assert isinstance(captured["env"], dict)
    assert "PATH" in captured["env"]
    assert "HOME" not in captured["env"]


def test_real_python_pytest_version_smoke_test_uses_subprocess_argv(tmp_path: Path) -> None:
    result = run(
        ("python", "-m", "pytest", "--version"),
        working_directory=tmp_path,
        workspace_root=tmp_path,
        timeout_seconds=30,
    )

    assert result.passed is True
    assert result.exit_code == 0
    assert result.argv == ("python", "-m", "pytest", "--version")
    assert "pytest" in result.stdout.lower()


def test_output_is_bounded_and_evidence_contains_stable_fields(tmp_path: Path) -> None:
    def noisy_runner(
        argv: tuple[str, ...],
        *,
        cwd: Path,
        env: dict[str, str],
        timeout_seconds: float,
    ) -> subprocess.CompletedProcess[str]:
        del argv, cwd, env, timeout_seconds
        return subprocess.CompletedProcess(
            args=["npm", "--version"],
            returncode=0,
            stdout="a" * 120,
            stderr="b" * 120,
        )

    result = run(
        ("npm", "--version"),
        working_directory=tmp_path,
        workspace_root=tmp_path,
        runner=noisy_runner,
        output_limit_bytes=32,
    )

    evidence = result.to_evidence()
    assert isinstance(result, VerificationResult)
    assert evidence["command_hash"] == result.command_hash
    assert evidence["exit_code"] == 0
    assert evidence["passed"] is True
    assert isinstance(evidence["elapsed_ms"], int)
    assert evidence["elapsed_ms"] >= 0
    assert evidence["working_directory"] == str(tmp_path.resolve())
    assert evidence["argv"] == ("npm", "--version")
    assert evidence["stdout"] == "a" * 32 + "...[truncated 88 bytes]"
    assert evidence["stderr"] == "b" * 32 + "...[truncated 88 bytes]"


def test_failed_allowed_command_returns_result_instead_of_raising(tmp_path: Path) -> None:
    def failing_runner(
        argv: tuple[str, ...],
        *,
        cwd: Path,
        env: dict[str, str],
        timeout_seconds: float,
    ) -> subprocess.CompletedProcess[str]:
        del argv, cwd, env, timeout_seconds
        return subprocess.CompletedProcess(
            args=["pnpm", "test"],
            returncode=1,
            stdout="",
            stderr="test failed",
        )

    result = run(
        ("pnpm", "test"),
        working_directory=tmp_path,
        workspace_root=tmp_path,
        runner=failing_runner,
    )

    assert result.exit_code == 1
    assert result.passed is False
    assert result.stderr == "test failed"


@pytest.mark.parametrize(
    ("raised", "expected_stderr"),
    (
        (
            subprocess.TimeoutExpired(
                cmd=["python", "-m", "pytest", "--version"], timeout=1, output="secret-out"
            ),
            "command timed out after 1 seconds",
        ),
        (FileNotFoundError("missing binary"), "command startup failed: FileNotFoundError"),
        (PermissionError("permission denied"), "command startup failed: PermissionError"),
        (OSError("exec format error"), "command startup failed: OSError"),
    ),
)
def test_subprocess_startup_and_timeout_failures_return_bounded_failed_result(
    tmp_path: Path,
    raised: subprocess.TimeoutExpired | OSError,
    expected_stderr: str,
) -> None:
    secret = "super-secret-token"

    def raising_runner(
        argv: tuple[str, ...],
        *,
        cwd: Path,
        env: dict[str, str],
        timeout_seconds: float,
    ) -> subprocess.CompletedProcess[str]:
        del argv, cwd, env, timeout_seconds
        raise raised

    result = run(
        ("pnpm", "install", f"--token={secret}"),
        working_directory=tmp_path,
        workspace_root=tmp_path,
        runner=raising_runner,
        safe_env={"API_TOKEN": secret},
        output_limit_bytes=48,
    )

    evidence = result.to_evidence()
    assert result.passed is False
    assert result.exit_code != 0
    assert result.stdout == ""
    assert expected_stderr in result.stderr
    assert len(result.stderr.encode("utf-8")) <= 80
    assert result.argv == ("pnpm", "install", "--token=[REDACTED]")
    assert secret not in str(evidence)
    assert evidence["command_hash"] == result.command_hash
    assert evidence["working_directory"] == str(tmp_path.resolve())


def test_secret_values_are_redacted_from_argv_env_and_output(tmp_path: Path) -> None:
    secret = "super-secret-token"

    def leaking_runner(
        argv: tuple[str, ...],
        *,
        cwd: Path,
        env: dict[str, str],
        timeout_seconds: float,
    ) -> subprocess.CompletedProcess[str]:
        del cwd, env, timeout_seconds
        return subprocess.CompletedProcess(
            args=list(argv),
            returncode=0,
            stdout=f"token={secret}",
            stderr=f"password={secret}",
        )

    result = run(
        ("pnpm", "install", f"--token={secret}"),
        working_directory=tmp_path,
        workspace_root=tmp_path,
        runner=leaking_runner,
        safe_env={"SAFE_FLAG": "1", "API_TOKEN": secret},
    )

    evidence = result.to_evidence()
    assert secret not in str(evidence)
    assert evidence["argv"] == ("pnpm", "install", "--token=[REDACTED]")
    assert evidence["stdout"] == "token=[REDACTED]"
    assert evidence["stderr"] == "password=[REDACTED]"
    assert evidence["env_keys"] == ("PATH", "SAFE_FLAG")


def test_secret_like_output_is_redacted_without_argv_or_env_secret_values(
    tmp_path: Path,
) -> None:
    openai_key = "sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890"
    github_token = "github_pat_11ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_abcdEFGHijklMNOP"
    slack_token = "xoxp-123456789012-123456789012-123456789012-abcdefABCDEF123456"
    bearer_jwt = (
        "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
        "eyJzdWIiOiIxMjM0NTY3ODkwIn0."
        "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    )
    bare_jwt = (
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
        "eyJpc3MiOiJ2ZXJpZmllciIsInN1YiI6IjEyMzQ1Njc4OTAifQ."
        "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    )
    empty_signature_jwt = "eyJhbGciOiJub25lIn0.eyJzdWIiOiIxMjMifQ."
    short_signature_jwt = "eyJhbGciOiJub25lIn0.eyJzdWIiOiIxMjMifQ.sig"
    private_key = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC\n"
        "-----END RSA PRIVATE KEY-----"
    )

    def secret_like_runner(
        argv: tuple[str, ...],
        *,
        cwd: Path,
        env: dict[str, str],
        timeout_seconds: float,
    ) -> subprocess.CompletedProcess[str]:
        del argv, cwd, env, timeout_seconds
        return subprocess.CompletedProcess(
            args=["pnpm", "test"],
            returncode=1,
            stdout=(
                "stdout before\n"
                f"openai {openai_key}\n"
                f"github {github_token}\n"
                f"slack {slack_token}\n"
                f"auth {bearer_jwt}\n"
                f"stdout jwt {bare_jwt}\n"
                f"empty signature jwt {empty_signature_jwt}\n"
                f"url https://alice:url-password@example.test/repo.git\n"
                "token=literal-token\n"
                "stdout after"
            ),
            stderr=(
                "stderr before\n"
                f"stderr jwt {bare_jwt}\n"
                f"short signature jwt {short_signature_jwt}\n"
                f"{private_key}\n"
                "api_key: literal-api-key\n"
                "password=literal-password\n"
                "secret: literal-secret\n"
                "stderr after"
            ),
        )

    result = run(
        ("pnpm", "test"),
        working_directory=tmp_path,
        workspace_root=tmp_path,
        runner=secret_like_runner,
    )
    persisted_output = result.stdout + result.stderr

    assert "stdout before" in result.stdout
    assert "stdout after" in result.stdout
    assert "stderr before" in result.stderr
    assert "stderr after" in result.stderr
    for secret in (
        openai_key,
        github_token,
        slack_token,
        bearer_jwt,
        bare_jwt,
        empty_signature_jwt,
        short_signature_jwt,
        "url-password",
        "literal-token",
        "literal-api-key",
        "literal-password",
        "literal-secret",
        "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC",
    ):
        assert secret not in persisted_output


def test_verification_result_is_immutable(tmp_path: Path) -> None:
    result = run(
        ("npm", "--version"),
        working_directory=tmp_path,
        workspace_root=tmp_path,
        runner=fake_runner,
    )

    with pytest.raises(AttributeError):
        result.passed = False  # type: ignore[misc]
