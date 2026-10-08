"""The Helm release's Recipient Key: one Secret, created by the first Runner (issue 46, 22 A4).

A Helm release is one *installation*, so its N replicas must register one key -- and Helm
cannot mint an X25519 pair in a template. The chart's init container runs
``agentic-runner recipient-key ensure-secret`` instead: read the Secret, create it if it
is absent, and hand the pair to this pod's ``RecipientKeyStore`` as an installer-managed
key. Two replicas starting at once both try to create; the API's 409 sends the loser
back to read what the winner wrote, so exactly one key exists per release.

The Kubernetes API is spoken to directly over httpx with the pod's projected
ServiceAccount token: a client library for two calls on one resource would be a
dependency in a package that ships to clients (ADR-0013 §4). The Role the chart grants
is ``get`` on that one Secret name and ``create`` on Secrets, nothing wider.
"""

from __future__ import annotations

import base64
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import httpx

from agentic_runner.sealed_box import RecipientKeyPair, RecipientKeyStore, generate_recipient_key

__all__ = ["KubernetesSecrets", "ensure_recipient_key", "in_cluster"]

SERVICE_ACCOUNT_DIR: Final[Path] = Path("/var/run/secrets/kubernetes.io/serviceaccount")
_KEYS: Final[tuple[str, ...]] = ("key_id", "public_key", "private_key")


class KubernetesSecrets:
    """Read and create Secrets in one namespace, as the pod's ServiceAccount."""

    def __init__(
        self,
        *,
        base_url: str,
        kube_ns: str,
        token: str,
        client: httpx.Client,
    ) -> None:
        self._url = f"{base_url.rstrip('/')}/api/v1/namespaces/{kube_ns}/secrets"
        self._headers = {"authorization": f"Bearer {token}", "content-type": "application/json"}
        self._client = client

    def get(self, name: str) -> RecipientKeyPair | None:
        response = self._client.get(f"{self._url}/{name}", headers=self._headers)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        data = response.json().get("data") or {}
        values = {key: _decode(data.get(key)) for key in _KEYS}
        if not all(values.values()):
            raise RuntimeError(f"Secret {name} exists but does not hold a Recipient Key")
        return RecipientKeyPair(**values)

    def create(self, name: str, pair: RecipientKeyPair) -> bool:
        """Create the Secret; ``False`` when another replica got there first."""

        body = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": name, "labels": {"app.kubernetes.io/name": "agentic-runner"}},
            "type": "Opaque",
            "data": {
                "key_id": _encode(pair.key_id),
                "public_key": _encode(pair.public_key),
                "private_key": _encode(pair.private_key),
            },
        }
        response = self._client.post(self._url, headers=self._headers, json=body)
        if response.status_code == 409:
            return False
        response.raise_for_status()
        return True


def in_cluster(*, service_account_dir: Path = SERVICE_ACCOUNT_DIR) -> tuple[KubernetesSecrets, str]:
    """The API as this pod sees it, and the Kubernetes namespace the pod is in."""

    host = _env("KUBERNETES_SERVICE_HOST")
    port = _env("KUBERNETES_SERVICE_PORT")
    kube_ns = (service_account_dir / "namespace").read_text(encoding="utf-8").strip()
    token = (service_account_dir / "token").read_text(encoding="utf-8").strip()
    client = httpx.Client(verify=str(service_account_dir / "ca.crt"), timeout=10.0)
    return (
        KubernetesSecrets(
            base_url=f"https://{host}:{port}",
            kube_ns=kube_ns,
            token=token,
            client=client,
        ),
        kube_ns,
    )


def ensure_recipient_key(
    api: KubernetesSecrets,
    *,
    secret_name: str,
    kube_ns: str,
    state_dir: Path,
    now: datetime | None = None,
) -> RecipientKeyPair:
    """The release's key, created once and installed into this pod's state directory."""

    pair = api.get(secret_name)
    if pair is None:
        fresh = generate_recipient_key()
        pair = fresh if api.create(secret_name, fresh) else api.get(secret_name)
        if pair is None:
            raise RuntimeError(f"Secret {secret_name} vanished between create and read")
    RecipientKeyStore(state_dir).install(
        pair, managed_by=f"secret:{kube_ns}/{secret_name}", now=now or datetime.now(UTC)
    )
    return pair


def _env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is not set; not running inside a Kubernetes pod")
    return value


def _encode(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def _decode(value: object) -> str:
    return base64.b64decode(str(value)).decode("utf-8") if value else ""
