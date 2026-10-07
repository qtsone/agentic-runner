#!/usr/bin/env bash
# The chart test (PRD issue 46 AC 1), against a kind cluster that already holds the
# Runner image as agentic-runner:test (`kind load docker-image agentic-runner:test`):
# install with replicas: 2 and assert both pods register as distinct Runners in the fake
# control plane, under one Recipient Key the first replica's init container created.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
chart="$(cd "${here}/.." && pwd)"
ns=runner-test
release=rel

kubectl apply -f "${here}/manifests.yaml"
kubectl -n "${ns}" create configmap fake-control-plane \
  --from-file="${here}/fake-control-plane.py" --dry-run=client -o yaml | kubectl apply -f -
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
  --wait --timeout 5m

# Ready means registered, heard and polling (agentic_runner.service.Readiness).
kubectl -n "${ns}" rollout status "statefulset/${release}-agentic-runner" --timeout=180s

stats="$(kubectl -n "${ns}" exec deployment/fake-control-plane -- \
  python -c 'import json,urllib.request; print(urllib.request.urlopen("http://127.0.0.1:8000/stats").read().decode())')"
echo "fake control plane: ${stats}"

python3 - "${stats}" <<'PY'
import json, sys
stats = json.loads(sys.argv[1])
assert stats["bootstraps"] == 2, f"expected two registrations, saw {stats['bootstraps']}"
assert len(set(stats["runner_ids"])) == 2, "the two pods must be two distinct Runners"
assert len(stats["recipient_key_ids"]) == 1, f"one Recipient Key per release, saw {stats['recipient_key_ids']}"
assert stats["isolation_modes"] == ["contract_uid"], stats["isolation_modes"]
assert len(stats["heartbeat_runner_ids"]) == 2, "both Runners must have heartbeat"
print("both replicas registered as distinct Runners under one Recipient Key")
PY

kubectl -n "${ns}" get secret "${release}-agentic-runner-recipient-key" -o jsonpath='{.data.key_id}' | base64 -d
echo
# The five capabilities and the 0400 credential mount are what the live objects carry, not
# only what the template renders.
kubectl -n "${ns}" get statefulset "${release}-agentic-runner" \
  -o jsonpath='{.spec.template.spec.containers[0].securityContext.capabilities.add}' \
  | grep -q '"SETUID","SETGID","CHOWN","FOWNER","DAC_OVERRIDE"'
echo "chart test passed"
