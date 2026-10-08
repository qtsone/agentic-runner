#!/usr/bin/env sh
# The image test for PRD issue 46: the barebones Runner image carries python, git, codex
# and claude and none of pytest, buf or alembic, and a contract_uid Runner without
# CAP_SETUID refuses to start. Usage: check-runner-images.sh <runner image>
set -eu

runner="$1"

echo "== ${runner}: the four tools are present"
docker run --rm --entrypoint sh "${runner}" -c '
  set -e
  for tool in python git codex claude; do command -v "$tool" >/dev/null || { echo "missing: $tool"; exit 1; }; done
  agentic-runner --version
  codex --version
  claude --version
'
echo "== ${runner}: no test tooling, no buf, no Alembic"
docker run --rm --entrypoint sh "${runner}" -c '
  set -e
  ! python -c "import pytest" 2>/dev/null || { echo "pytest is in the barebones image"; exit 1; }
  ! python -c "import alembic" 2>/dev/null || { echo "alembic is in the barebones image"; exit 1; }
  ! command -v buf >/dev/null || { echo "buf is in the barebones image"; exit 1; }
'
echo "== ${runner}: contract_uid without CAP_SETUID refuses to start, naming the capability"
output="$(docker run --rm --cap-drop ALL -e AGENTIC_CONTROL_PLANE_URL=http://unused "${runner}" run 2>&1 || true)"
echo "${output}" | grep -q "CAP_SETUID" || { echo "expected a refusal naming CAP_SETUID, got: ${output}"; exit 1; }
echo "== ok"
