# Contributing

Thank you for helping. Before your first pull request is merged you sign the
[Contributor License Agreement](.github/CLA.md) once, by commenting on the PR as the CLA check
asks: `I have read the CLA Document and I hereby sign the CLA`. `recheck` re-runs the check.

## Setup

Python 3.12 and [uv](https://docs.astral.sh/uv/):

```bash
uv sync
uv run pytest -q
```

Some tests need more than Python and skip themselves without it: the chart template test needs
`helm`, the Contract uid isolation test needs root (`CAP_SETUID`), and the Codex MCP isolation
test needs the `codex` CLI pinned in `Dockerfile.runner`. CI runs all of them.

## Before you open a pull request

```bash
uv run ruff check packages/ tests/
uv run ruff format --check packages/ tests/
uv run mypy packages/
uv run pytest -q
```

- Python 3.12+, strict mypy, ruff line length 100.
- Async tests are marked per test with `@pytest.mark.asyncio`.
- Use the vocabulary in [CONTEXT.md](CONTEXT.md), and avoid the synonyms it lists.
- Commits follow [Conventional Commits](https://www.conventionalcommits.org/).

## Two rules the tests enforce

- **The partition.** Each package imports only itself, the standard library and the
  distributions its `pyproject.toml` declares (`tests/test_package_partition.py`). The Runner
  never imports the control plane.
- **The compatibility floor.** `agentic-runner-contracts` is the wire every installed Runner
  speaks. Any change to it is versioned: additive changes bump the minor, anything else the
  major — record why next to `__version__` in
  `packages/contracts/src/agentic_runner_contracts/__init__.py`.
