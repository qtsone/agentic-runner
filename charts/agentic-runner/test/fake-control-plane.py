"""The fake control plane the chart test registers against (PRD issue 46).

Stdlib plus ``cryptography`` (which the Runner image already ships), so it runs from the
image itself: it answers bootstrap and heartbeat the way the platform would -- a fresh
identity per bootstrap, one Temporal namespace for the release -- and reports what it saw
on ``/stats``: how many Runners registered, their ids, and the Recipient Key each presented.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

NAMESPACE = os.environ.get("FAKE_NAMESPACE", "org-00000000-0000-0000-0000-000000000001")
CONTRACTS = os.environ.get("FAKE_CONTRACTS_VERSION", "1.0.0")
STATE: dict[str, list[dict[str, object]]] = {"bootstraps": [], "heartbeats": []}


def _pem() -> str:
    return (
        Ed25519PrivateKey.generate()
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        .decode("ascii")
    )


class Handler(BaseHTTPRequestHandler):
    def _json(self, status: int, body: dict[str, object]) -> None:
        raw = json.dumps(body, default=str).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802 - http.server's contract
        if self.path == "/stats":
            self._json(
                200,
                {
                    "bootstraps": len(STATE["bootstraps"]),
                    "runner_ids": [b["runner_id"] for b in STATE["bootstraps"]],
                    "recipient_key_ids": sorted(
                        {str(b["recipient_key_id"]) for b in STATE["bootstraps"]}
                    ),
                    "isolation_modes": sorted({str(b["isolation"]) for b in STATE["bootstraps"]}),
                    "heartbeats": len(STATE["heartbeats"]),
                    "heartbeat_runner_ids": sorted({str(h["runner_id"]) for h in STATE["heartbeats"]}),
                },
            )
            return
        self._json(404, {"detail": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - http.server's contract
        length = int(self.headers.get("content-length", "0") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        expires = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        if self.path == "/api/runner/v1/runners/bootstrap":
            runner_id = str(uuid4())
            STATE["bootstraps"].append(
                {
                    "runner_id": runner_id,
                    "recipient_key_id": body["recipient_key"]["key_id"],
                    "isolation": body["isolation_mode"],
                }
            )
            print(f"bootstrap {runner_id} isolation={body['isolation_mode']}", flush=True)
            self._json(
                200,
                {
                    "identity": {
                        "runner_id": runner_id,
                        "identity_id": f"identity-{runner_id}",
                        "private_key_pem": _pem(),
                    },
                    "temporal_namespace": NAMESPACE,
                    "task_queue": f"runner.{runner_id}",
                    "runner_token": f"runner-token-{runner_id}",
                    "runner_token_expires_at": expires,
                    "tag_set_version": 1,
                    "floor_state": "ok",
                    "contracts_floor": CONTRACTS,
                    "host_party": "organisation",
                },
            )
            return
        if self.path == "/api/runner/v1/runners/heartbeat":
            runner_id = self.headers.get("X-Runner-Id", "")
            STATE["heartbeats"].append({"runner_id": runner_id, "isolation": body["isolation_mode"]})
            self._json(
                200,
                {
                    "runner_id": runner_id,
                    "runner_token": f"runner-token-{runner_id}-{len(STATE['heartbeats'])}",
                    "runner_token_expires_at": expires,
                    "floor_state": "ok",
                    "contracts_floor": CONTRACTS,
                    "tag_set_version": 1,
                    "accepts_new_directives": True,
                },
            )
            return
        self._json(404, {"detail": "not found"})


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    print(f"fake control plane on :{port}, namespace {NAMESPACE}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
