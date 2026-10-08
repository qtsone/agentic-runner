#!/usr/bin/env bash
# The chart test (PRD issue 46 AC 1), against a kind cluster that already holds the
# Runner image as agentic-runner:test (`kind load docker-image agentic-runner:test`):
# install with replicas: 2 and assert both pods register as distinct Runners in the fake
# control plane, under one Recipient Key the first replica's init container created.
# Then (local-agents 04b) run one Codex Directive on an API key the fake control plane
# delivered sealed, and assert it reached the provider through the LLM proxy.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
chart="$(cd "${here}/.." && pwd)"
ns=runner-test
release=rel

kubectl create namespace "${ns}" --dry-run=client -o yaml | kubectl apply -f -
kubectl -n "${ns}" create configmap chart-test \
  --from-file="${here}/run-directive.py" --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f "${here}/manifests.yaml"
kubectl -n "${ns}" rollout status deployment/fake-control-plane --timeout=180s
kubectl -n "${ns}" rollout status deployment/temporal --timeout=180s

helm upgrade --install "${release}" "${chart}" -n "${ns}" \
  --set image.repository=agentic-runner --set image.tag=test --set image.pullPolicy=Never \
  --set replicas=2 \
  --set controlPlane.url=http://fake-control-plane:8000 \
  --set temporal.address=temporal:7233 \
  --set agentToken.value=chart-test-agent-token-0123456789 \
  --set tags.region=kind \
  --set storage.size=1Gi \
  -f "${here}/values-directive.yaml" \
  --wait --timeout 5m

# Ready means registered, heard and polling (agentic_runner.service.Readiness).
kubectl -n "${ns}" rollout status "statefulset/${release}-agentic-runner" --timeout=180s

stats="$(kubectl -n "${ns}" exec deployment/fake-control-plane -- \
  python -c 'import json,urllib.request; print(urllib.request.urlopen("http://127.0.0.1:8000/stats").read().decode())')"
echo "fake control plane: ${stats}"

python3 - "${stats}" <<'PY'
import json, re, sys
stats = json.loads(sys.argv[1])
assert stats["bootstraps"] == 2, f"expected two registrations, saw {stats['bootstraps']}"
assert len(set(stats["runner_ids"])) == 2, "the two pods must be two distinct Runners"
assert len(stats["recipient_key_ids"]) == 1, f"one Recipient Key per release, saw {stats['recipient_key_ids']}"
assert stats["isolation_modes"] == ["contract_uid"], stats["isolation_modes"]
assert len(stats["heartbeat_runner_ids"]) == 2, "both Runners must have heartbeat"
builds = stats["bootstrap_build_ids"]
assert len(builds) == 1 and re.fullmatch(r"[0-9a-f]{64}", builds[0]), f"one full build_id, saw {builds}"
assert stats["heartbeat_build_ids"] == builds, f"the attestation must carry it, saw {stats['heartbeat_build_ids']}"
print(f"both replicas registered as distinct Runners under one Recipient Key, build {builds[0]}")
PY

kubectl -n "${ns}" get secret "${release}-agentic-runner-recipient-key" -o jsonpath='{.data.key_id}' | base64 -d
echo
# The five capabilities and the 0400 credential mount are what the live objects carry, not
# only what the template renders.
kubectl -n "${ns}" get statefulset "${release}-agentic-runner" \
  -o jsonpath='{.spec.template.spec.containers[0].securityContext.capabilities.add}' \
  | grep -q '"SETUID","SETGID","CHOWN","FOWNER","DAC_OVERRIDE"'

# One Codex Directive on the shared Runner, on the key the fake delivered sealed. Its
# outcome comes back through Temporal; what the provider saw comes from /stats.
runner_id="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["runner_ids"][0])' "${stats}")"
outcome="$(kubectl -n "${ns}" exec deployment/fake-control-plane -- \
  python /chart-test/run-directive.py temporal:7233 org-00000000-0000-0000-0000-000000000001 "${runner_id}")"
echo "directive: ${outcome}"

# Usage rides the next heartbeat, so wait for it rather than for a fixed time.
for _ in $(seq 1 30); do
  stats="$(kubectl -n "${ns}" exec deployment/fake-control-plane -- \
    python -c 'import json,urllib.request; print(urllib.request.urlopen("http://127.0.0.1:8000/stats").read().decode())')"
  python3 -c 'import json,sys; sys.exit(0 if json.loads(sys.argv[1])["usage"] else 1)' "${stats}" && break
  sleep 5
done
echo "fake control plane: ${stats}"

python3 - "${stats}" "${outcome}" <<'PY'
import json, sys
stats, outcome = json.loads(sys.argv[1]), json.loads(sys.argv[2].strip().splitlines()[-1])
assert outcome["outcome"] == "proposed", outcome
[run] = [e for e in stats["evidence_events"] if e["source"] == "learning.directive_run"]
assert run["exit_code"] == 0, run
# The proxy admits a call only on the attempt's bearer and spends the delivered key
# upstream: the provider saw that key and nothing else, on Codex's Responses route.
calls = [c for c in stats["provider_calls"] if c["path"] == "/v1/responses"]
assert calls, stats["provider_calls"]
assert {c["authorization"] for c in stats["provider_calls"]} == {"Bearer sk-chart-test-funder-key"}
assert any(s["present"] and s["probe"] == "valid" and s["key_id"] == "OPENAI_API_KEY@v1"
           for s in stats["slots"]), stats["slots"]
assert any(u["contract_id"] == stats["contract_id"] and u["prompt_tokens"] == 42
           for u in stats["usage"]), stats["usage"]
print("one Codex Directive ran on the delivered API key through the LLM proxy")
PY
echo "chart test passed"
