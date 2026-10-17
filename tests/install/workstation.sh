#!/usr/bin/env bash
# docs/workstation.md, run as written: `uv tool install` the built wheels, `agentic-runner
# install <name>`, and a Runner that registers and heartbeats against the conformance kit's
# fake control plane and a Temporal dev server (`temporal` on PATH).
# Usage: tests/install/workstation.sh <dist dir holding both wheels> process|launchd
#
# process  `install --no-start`, then the exact command the login agent runs, as a plain
#          process: what a Linux host without a user systemd instance (a CI runner) can do.
# launchd  `install` writes the LaunchAgent and bootstraps it into gui/<uid>; the plist is
#          linted and read back, and `stop` boots it out.
#
# The vendor CLI is a stub: `install` only needs one of `codex` / `claude` on PATH, and no
# Directive runs here.
set -euo pipefail

dist="$(cd "$1" && pwd)"
mode="$2"
repo="$(cd "$(dirname "$0")/../.." && pwd)"
work="$(mktemp -d)"
name=ci
root="${work}/root"
namespace=org-00000000-0000-0000-0000-000000000001
stats=http://127.0.0.1:8000/stats
pids=()
# A tool directory of its own, so a run on a developer machine leaves their tools alone.
export UV_TOOL_DIR="${work}/tools" UV_TOOL_BIN_DIR="${work}/tools/bin"

cleanup() {
  if [ "${mode}" = launchd ]; then agentic-runner stop "${name}" --root "${root}" || true; fi
  for pid in ${pids[@]+"${pids[@]}"}; do kill "${pid}" 2>/dev/null || true; done
  if [ -f "${root}/${name}/runner.log" ]; then echo "-- runner.log"; cat "${root}/${name}/runner.log"; fi
}
trap cleanup EXIT

echo "== uv tool install, from the wheels alone"
# --no-cache: a branch builds the same version as main until release, and a cached wheel of
# that version would install main's code instead of this build.
uv tool install --no-cache --python 3.12 --find-links "${dist}" agentic-runner
export PATH="${UV_TOOL_BIN_DIR}:${PATH}"
agentic-runner --version

temporal server start-dev --port 7233 --headless --namespace "${namespace}" \
  > "${work}/temporal.log" 2>&1 &
pids+=($!)
# The conformance kit's fake control plane, served from the Runner just installed.
"${UV_TOOL_DIR}/agentic-runner/bin/python" -m agentic_runner.testing 8000 \
  --namespace "${namespace}" > "${work}/fake.log" 2>&1 &
pids+=($!)
for _ in $(seq 60); do
  curl -fsS "${stats}" >/dev/null 2>&1 && nc -z 127.0.0.1 7233 && break
  sleep 1
done

mkdir -p "${work}/bin"
printf '#!/bin/sh\necho "2.1.280 (Claude Code)"\n' > "${work}/bin/claude"
chmod +x "${work}/bin/claude"
export PATH="${work}/bin:${PATH}"

echo "== agentic-runner install ${name} (${mode})"
start_flag=()
[ "${mode}" = process ] && start_flag=(--no-start)
AGENTIC_RUNNER_REGISTRATION_TOKEN=workstation-test-registration-token agentic-runner install "${name}" \
  --control-plane http://127.0.0.1:8000 \
  --temporal-address 127.0.0.1:7233 --temporal-plaintext \
  --root "${root}" ${start_flag[@]+"${start_flag[@]}"}

if [ "${mode}" = launchd ]; then
  plist="${HOME}/Library/LaunchAgents/agentic-runner.${name}.plist"
  plutil -lint "${plist}"
  [ "$(plutil -extract Label raw "${plist}")" = "agentic-runner.${name}" ]
  [ "$(plutil -extract RunAtLoad raw "${plist}")" = true ]
  [ "$(plutil -extract KeepAlive.SuccessfulExit raw "${plist}")" = false ]
  [ "$(plutil -extract ProgramArguments.1 raw "${plist}")" = run ]
  [ "$(plutil -extract ProgramArguments.3 raw "${plist}")" = "${name}" ]
  launchctl print "gui/$(id -u)/agentic-runner.${name}" | grep -E 'state|pid'
else
  # The command the login agent runs (workstation.runner_command), minus --login-agent.
  agentic-runner run --name "${name}" --root "${root}" &
  runner_pid=$!
  pids+=("${runner_pid}")
fi

python3 "${repo}/tests/install/await_runner.py" "${stats}" --isolation none
agentic-runner status "${name}" --root "${root}" | tee "${work}/status"
grep -q 'process        running' "${work}/status"
grep -qE 'heartbeat      [0-9]+s ago' "${work}/status"

echo "== stop drains to a clean exit"
if [ "${mode}" = launchd ]; then
  agentic-runner stop "${name}" --root "${root}"
  for _ in $(seq 30); do
    agentic-runner status "${name}" --root "${root}" | grep -q 'not running' && break
    sleep 1
  done
  agentic-runner status "${name}" --root "${root}" | grep -q 'process        not running'
else
  kill -TERM "${runner_pid}"
  wait "${runner_pid}"
fi
echo "workstation test passed (${mode})"
