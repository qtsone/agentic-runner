"""``python -m agentic_runner.testing [port]``: serve the fake control plane until killed.

What the chart, Docker and workstation tests register a Runner against; ``GET /stats``
reports what it saw.
"""

from __future__ import annotations

import argparse

from agentic_runner.testing.control_plane import DEFAULT_NAMESPACE, FakeControlPlane


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m agentic_runner.testing")
    parser.add_argument("port", type=int, nargs="?", default=8000)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--namespace", default=DEFAULT_NAMESPACE)
    parser.add_argument("--host-party", default="organisation")
    arguments = parser.parse_args()

    plane = FakeControlPlane(namespace=arguments.namespace, host_party=arguments.host_party)
    server = plane.server(arguments.host, arguments.port)
    print(
        f"fake control plane on :{server.server_address[1]}, namespace {arguments.namespace}",
        flush=True,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
