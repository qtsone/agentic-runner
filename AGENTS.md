# AGENTS.md

Guidance for coding agents (Claude Code, Codex and any AGENTS.md-aware tool) working in this
repository. It is the single canonical instructions file; `CLAUDE.md` is a symlink to it.

## What this is

The Agentic OS **Runner**: an activity-only Temporal worker that executes the steps of the Ralph
Loop that must run where the work is — the Workspace, the Agent Runtime subprocess, the verb
seams, the credentials the Runner holds. The workflows, Gates, Grants authority and Evidence
sink live in the control plane, which is not in this repository. See `README.md`.

**Read `CONTEXT.md` before writing code.** It is the glossary of the terms the code uses
(Runner, Contract, Directive, Grant, Agent Runtime, Recipient Key, Credential Reference…), each
with the synonyms to avoid. Use those terms.

## Layout

- `packages/contracts/` — `agentic-runner-contracts`: the wire between the control plane and a
  Runner. Its version is the compatibility floor (`README.md`); every change to it is a
  versioned, deliberate change, recorded next to `__version__`.
- `packages/runner/` — `agentic-runner`: the Runner process (`agentic_runner/service.py`, console
  script `agentic-runner`), the activities (`activities.py`), the Agent Runtime port and its
  Codex and Claude Code implementations (`workers/`), the git Workspace and GitHub client
  (`integrations/`), the LLM proxy and the workstation install.
- `charts/agentic-runner/` — the Helm chart; `test/run.sh` is the kind test.
- `Dockerfile.runner`, `scripts/check-runner-images.sh` — the barebones image and its test.
- `tests/` — unit and integration tests; `tests/test_package_partition.py` is the partition gate.
- `packages/runner/src/agentic_runner/testing/` — the conformance kit: the fake control plane
  every test registers against, and the scenario suite (`docs/conformance.md`).

## Commands

```bash
uv sync
uv run pytest -q
uv run pytest tests/unit/test_runner_service.py -v
uv run pytest --pyargs agentic_runner.testing      # the conformance scenarios
uv run ruff check packages/ tests/
uv run ruff format --check packages/ tests/
uv run mypy packages/
docker build -f Dockerfile.runner -t agentic-runner:dev .
./scripts/check-runner-images.sh agentic-runner:dev
```

## Rules

- **The partition.** Each package imports only itself, the standard library and what its
  `pyproject.toml` declares. Contracts never imports the Runner; neither imports the control
  plane.
- **No state in the Runner's process.** What one activity learns travels in its output and back
  in the next input — never a cache keyed by Work Record.
- **Integration ports and fakes.** Each integration is a protocol with a real client and a
  `fake_*` implementation; tests use the fakes.
- Python 3.12+, strict mypy, ruff (line length 100, rules `E,F,I,B,UP,N,SIM`), `pytest-asyncio`
  with per-test `@pytest.mark.asyncio`. Explicit error propagation, no silent exception
  swallowing.
- Comments are rationale-first and cite the ADR or issue behind a non-obvious decision; those
  numbers refer to the Agentic OS design records.
