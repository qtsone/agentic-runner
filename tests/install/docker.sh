#!/usr/bin/env bash
# docs/docker.md, run as written against the fake control plane and a Temporal dev server.
# Usage: tests/install/docker.sh <runner image>
#
# 1. The doc's `docker run` without the five capabilities refuses to start, naming
#    CAP_SETUID, and registers nothing.
# 2. The doc's `docker run` registers a contract_uid Runner that heartbeats.
# 3. examples/compose.yaml does the same, and `docker compose stop` drains to exit 0.
# 4. examples/compose.isolation-none.yaml registers an isolation: none Runner as uid 65532
#    with no capabilities.
#
# runner.env is the example file with only the three site values replaced, so the posture
# lines a person copies are the ones this test runs.
set -euo pipefail

image="$1"
repo="$(cd "$(dirname "$0")/../.." && pwd)"
work="$(mktemp -d)"
network=runner-ci
namespace=org-00000000-0000-0000-0000-000000000001
stats=http://127.0.0.1:8000/stats
caps=(--cap-drop ALL --cap-add SETUID --cap-add SETGID --cap-add CHOWN --cap-add FOWNER
  --cap-add DAC_OVERRIDE)

cleanup() {
  docker rm -f agentic-runner fake-control-plane temporal >/dev/null 2>&1 || true
  (cd "${work}" && docker compose -f compose.yaml -f compose.ci.yaml \
    -f compose.isolation-none.yaml down -v >/dev/null 2>&1) || true
  docker volume rm agentic-runner-state >/dev/null 2>&1 || true
  docker network rm "${network}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

fresh_control_plane() {
  docker rm -f fake-control-plane >/dev/null 2>&1 || true
  docker run -d --name fake-control-plane --network "${network}" -p 8000:8000 \
    -v "${repo}/charts/agentic-runner/test/fake-control-plane.py:/fake.py:ro" \
    -e FAKE_NAMESPACE="${namespace}" --entrypoint python "${image}" /fake.py 8000 >/dev/null
  for _ in $(seq 30); do curl -fsS "${stats}" >/dev/null 2>&1 && return; sleep 1; done
  echo "fake control plane did not start"; docker logs fake-control-plane; exit 1
}

docker network create "${network}" >/dev/null
docker run -d --name temporal --network "${network}" temporalio/admin-tools:1.32.0 \
  temporal server start-dev --ip=0.0.0.0 --port=7233 --headless --namespace="${namespace}" \
  >/dev/null

cp "${repo}"/examples/compose.yaml "${repo}"/examples/compose.isolation-none.yaml "${work}/"
sed -e 's|^AGENTIC_CONTROL_PLANE_URL=.*|AGENTIC_CONTROL_PLANE_URL=http://fake-control-plane:8000|' \
  -e 's|^TEMPORAL_ADDRESS=.*|TEMPORAL_ADDRESS=temporal:7233|' \
  -e 's|^AGENTIC_TEMPORAL_TLS=.*|AGENTIC_TEMPORAL_TLS=false|' \
  -e 's|^AGENTIC_AGENT_TOKEN=.*|AGENTIC_AGENT_TOKEN=docker-test-agent-token-0123456789|' \
  "${repo}/examples/runner.env.example" > "${work}/runner.env"
# The published image, swapped for the one under test, and the test network beside the
# project's own.
cat > "${work}/compose.ci.yaml" <<YAML
services:
  runner:
    image: ${image}
    networks: [default, ci]
networks:
  ci:
    name: ${network}
    external: true
YAML

echo "== docker run without the capabilities refuses to start"
fresh_control_plane
set +e
output="$(docker run --rm --network "${network}" --env-file "${work}/runner.env" \
  -e AGENTIC_RUNNER_ISOLATION=contract_uid --cap-drop ALL "${image}" 2>&1)"
status=$?
set -e
echo "${output}"
[ "${status}" -ne 0 ] || { echo "expected a non-zero exit"; exit 1; }
grep -q CAP_SETUID <<< "${output}" || { echo "expected the refusal to name CAP_SETUID"; exit 1; }
curl -fsS "${stats}" | grep -q '"bootstraps": 0' || { echo "a refused start registered"; exit 1; }

echo "== docker run with the capabilities: contract_uid"
docker volume create agentic-runner-state >/dev/null
docker run -d --name agentic-runner --network "${network}" \
  --restart unless-stopped \
  --env-file "${work}/runner.env" \
  -e AGENTIC_RUNNER_ISOLATION=contract_uid \
  "${caps[@]}" \
  --read-only --tmpfs /tmp --tmpfs /run/agentic-runner \
  -v agentic-runner-state:/var/lib/agentic-os \
  --stop-timeout 2100 \
  "${image}" >/dev/null
python3 "${repo}/tests/install/await_runner.py" "${stats}" --isolation contract_uid \
  || { docker logs agentic-runner; exit 1; }
docker rm -f agentic-runner >/dev/null

compose=(docker compose --project-directory "${work}" -f "${work}/compose.yaml"
  -f "${work}/compose.ci.yaml")

echo "== examples/compose.yaml: contract_uid"
fresh_control_plane
"${compose[@]}" up -d
python3 "${repo}/tests/install/await_runner.py" "${stats}" --isolation contract_uid \
  || { "${compose[@]}" logs; exit 1; }
# An idle Runner drains at once, so a clean stop is quick and exits 0; a SIGKILL at the
# end of the grace period would be 137.
"${compose[@]}" stop --timeout 60
exit_code="$(docker inspect --format '{{.State.ExitCode}}' "$("${compose[@]}" ps -aq runner)")"
[ "${exit_code}" = 0 ] || { echo "expected the drain to exit 0, got ${exit_code}"; exit 1; }
"${compose[@]}" down -v

echo "== examples/compose.isolation-none.yaml: none, uid 65532, no capabilities"
fresh_control_plane
compose+=(-f "${work}/compose.isolation-none.yaml")
"${compose[@]}" up -d
python3 "${repo}/tests/install/await_runner.py" "${stats}" --isolation none \
  || { "${compose[@]}" logs; exit 1; }
container="$("${compose[@]}" ps -q runner)"
[ "$(docker exec "${container}" id -u)" = 65532 ] || { echo "expected uid 65532"; exit 1; }
docker exec "${container}" grep -q '^CapEff:\s*0000000000000000$' /proc/1/status \
  || { echo "expected no effective capabilities"; exit 1; }

echo "docker test passed"
