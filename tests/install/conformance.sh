#!/usr/bin/env bash
# docs/conformance.md, run as written: `agentic-runner[testing]` from the built wheels into
# a clean virtualenv, then the scenario suite against that install.
# Usage: tests/install/conformance.sh <dist dir holding both wheels>
#
# The wheels are installed by path, not by name: PyPI holds a release with the same
# version, and a resolver free to pick it would test that instead of this build.
set -euo pipefail

dist="$(cd "$1" && pwd)"
work="$(mktemp -d)"
venv="${work}/venv"

uv venv --python 3.12 "${venv}"
contracts=("${dist}"/agentic_runner_contracts-*.whl)
runner=("${dist}"/agentic_runner-*.whl)
uv pip install --python "${venv}" "${contracts[0]}" "${runner[0]}[testing]"

# From a directory outside the repository, so nothing but the install is on the path.
cd "${work}"
"${venv}/bin/python" -m pytest -p no:cacheprovider -v --pyargs agentic_runner.testing
echo "conformance suite passed"
