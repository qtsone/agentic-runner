"""The Runner Helm chart renders what PRD issue 46 says it must.

``helm template`` is the oracle: the rendered objects are parsed and asserted on, so a
value that stops reaching the StatefulSet fails here rather than on a cluster. Skipped
where no ``helm`` binary is on PATH; the runner-image workflow sets
``AGENTIC_OS_REQUIRE_HELM`` so the skip becomes a failure there and the coverage cannot
quietly disappear (the same posture as the contract-uid isolation job).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CHART = REPO_ROOT / "charts" / "agentic-runner"
FIVE_CAPABILITIES = ["SETUID", "SETGID", "CHOWN", "FOWNER", "DAC_OVERRIDE"]
REQUIRED = [
    "--set",
    "controlPlane.url=https://api.agentic.example.test",
    "--set",
    "temporal.address=temporal-grpc.example.test:443",
    "--set",
    "agentToken.value=agent-token-of-sixteen-plus-chars",
]

pytestmark = pytest.mark.skipif(
    shutil.which("helm") is None and not os.environ.get("AGENTIC_OS_REQUIRE_HELM"),
    reason="helm is not installed",
)


def _render(*extra: str) -> dict[tuple[str, str], dict[str, Any]]:
    rendered = subprocess.run(
        ["helm", "template", "rel", str(CHART), *REQUIRED, *extra],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    objects = [doc for doc in yaml.safe_load_all(rendered) if doc]
    return {(doc["kind"], doc["metadata"]["name"]): doc for doc in objects}


def _runner(objects: dict[tuple[str, str], dict[str, Any]]) -> dict[str, Any]:
    return objects[("StatefulSet", "rel-agentic-runner")]


def _env(container: dict[str, Any]) -> dict[str, Any]:
    return {entry["name"]: entry.get("value", entry.get("valueFrom")) for entry in container["env"]}


def test_the_documented_values_render_and_the_security_context_is_exactly_the_five() -> None:
    objects = _render(
        "--set",
        "replicas=2",
        "--set",
        "tags.region=eu-west-1",
        "--set",
        "tags.gpu=none",
        "--set",
        "credentials.existingSecret=runner-credentials",
        "--set",
        "hooks.runner_startup=#!/bin/sh",
        "--set",
        "maxConcurrentDirectives=3",
    )
    runner = _runner(objects)
    spec = runner["spec"]["template"]["spec"]
    [container] = spec["containers"]
    [init] = spec["initContainers"]

    assert runner["spec"]["replicas"] == 2
    # 17 A1: root with exactly the five, ALL dropped first, on the Runner and its init.
    for candidate in (container, init):
        assert candidate["securityContext"]["capabilities"] == {
            "drop": ["ALL"],
            "add": FIVE_CAPABILITIES,
        }
        assert candidate["securityContext"]["readOnlyRootFilesystem"] is True
    assert spec["securityContext"]["runAsUser"] == 0
    assert spec["securityContext"]["runAsNonRoot"] is False
    # 17 A11: Credential Reference Secrets mount 0400; hooks 0755 (issue 45).
    volumes = {volume["name"]: volume for volume in spec["volumes"]}
    assert volumes["credentials"]["secret"]["defaultMode"] == 0o400
    assert volumes["credentials"]["secret"]["secretName"] == "runner-credentials"
    assert volumes["hooks"]["configMap"]["defaultMode"] == 0o755
    assert objects[("ConfigMap", "rel-agentic-runner-hooks")]["data"] == {
        "runner_startup": "#!/bin/sh\n"
    }
    # Issue 02's drain, issue 41's token and 25 §9's Tags all land as environment.
    env = _env(container)
    assert spec["terminationGracePeriodSeconds"] == 2100
    assert container["lifecycle"]["preStop"]["exec"]["command"] == ["/bin/sh", "-c", "kill -TERM 1"]
    assert env["AGENTIC_AGENT_TOKEN"] == {
        "secretKeyRef": {"name": "rel-agentic-runner-agent-token", "key": "token"}
    }
    assert env["AGENTIC_RUNNER_TAGS"] == "gpu=none,region=eu-west-1"
    assert env["AGENTIC_RUNNER_ISOLATION"] == "contract_uid"
    assert env["AGENTIC_RUNNER_MAX_CONCURRENT_DIRECTIVES"] == "3"
    assert env["AGENTIC_RUNNER_CREDENTIAL_STORE"] == "/etc/agentic-runner/credentials"
    assert env["AGENTIC_RUNNER_HOOKS_PATH"] == "/etc/agentic-runner/hooks"
    assert container["readinessProbe"]["httpGet"]["path"] == "/healthz"
    # Per-replica state: an identity is the pod's own (ADR-0013 §8).
    [claim] = runner["spec"]["volumeClaimTemplates"]
    assert claim["metadata"]["name"] == "state"
    assert runner["spec"]["podManagementPolicy"] == "Parallel"


def test_the_recipient_key_is_ensured_by_an_init_container_with_a_minimal_role() -> None:
    objects = _render()
    spec = _runner(objects)["spec"]["template"]["spec"]
    [init] = spec["initContainers"]

    assert init["args"] == [
        "recipient-key",
        "ensure-secret",
        "--secret",
        "rel-agentic-runner-recipient-key",
    ]
    # The API token reaches the init container only; the Runner container mounts none.
    assert spec["automountServiceAccountToken"] is False
    assert any(mount["name"] == "kube-api" for mount in init["volumeMounts"])
    assert all(mount["name"] != "kube-api" for mount in spec["containers"][0]["volumeMounts"])
    role = objects[("Role", "rel-agentic-runner-recipient-key")]
    assert role["rules"] == [
        {
            "apiGroups": [""],
            "resources": ["secrets"],
            "resourceNames": ["rel-agentic-runner-recipient-key"],
            "verbs": ["get"],
        },
        {"apiGroups": [""], "resources": ["secrets"], "verbs": ["create"]},
    ]


def test_isolation_none_runs_unprivileged_with_no_capabilities() -> None:
    """17 A2: the mode a `restricted` namespace can run -- declared, never detected."""

    objects = _render("--set", "isolation=none")
    spec = _runner(objects)["spec"]["template"]["spec"]
    [container] = spec["containers"]

    assert spec["securityContext"]["runAsNonRoot"] is True
    assert spec["securityContext"]["runAsUser"] == 65532
    assert container["securityContext"]["capabilities"] == {"drop": ["ALL"]}
    assert container["securityContext"]["allowPrivilegeEscalation"] is False
    assert _env(container)["AGENTIC_RUNNER_ISOLATION"] == "none"


@pytest.mark.parametrize("isolation", ["contract_uid", "none"])
def test_the_runner_creates_its_own_state_directory(isolation: str) -> None:
    """Runner-repo 08: a kubelet-made `state` subPath is root's, with the volume's mode,
    and the Runner refuses it; mounting the volume root lets the Runner make it 0700."""

    spec = _runner(_render("--set", f"isolation={isolation}"))["spec"]["template"]["spec"]
    for container in [*spec["initContainers"], *spec["containers"]]:
        [root] = [
            mount
            for mount in container["volumeMounts"]
            if mount["mountPath"] == "/var/lib/agentic-os"
        ]
        assert root["name"] == "state"
        assert "subPath" not in root
        assert _env(container)["AGENTIC_RUNNER_STATE_DIR"] == "/var/lib/agentic-os/state"
    # fsGroup's default walk would add g+rw to the identity file on every mount.
    if "fsGroup" in spec["securityContext"]:
        assert spec["securityContext"]["fsGroupChangePolicy"] == "OnRootMismatch"


def test_an_unknown_isolation_mode_and_a_missing_token_are_refused() -> None:
    with pytest.raises(subprocess.CalledProcessError) as refused:
        _render("--set", "isolation=auto")
    assert "isolation must be contract_uid or none" in refused.value.stderr

    with pytest.raises(subprocess.CalledProcessError) as missing:
        subprocess.run(
            ["helm", "template", "rel", str(CHART), *REQUIRED[:4]],
            check=True,
            capture_output=True,
            text=True,
        )
    assert "agentToken.value or agentToken.existingSecret is required" in missing.value.stderr


def test_the_chart_test_fixtures_are_in_step_with_the_chart() -> None:
    """run.sh installs the chart under the names asserted above; a rename here without
    one there would pass locally and fail only on the kind job."""

    script = (CHART / "test" / "run.sh").read_text(encoding="utf-8")
    assert "--set replicas=2" in script
    assert "recipient-key" in script
    assert json.dumps(FIVE_CAPABILITIES, separators=(",", ":")).strip("[]") in script
