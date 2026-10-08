"""``python -m agentic_runner.testing [port]``: serve the fake control plane until killed.

What the chart, Docker and workstation tests register a Runner against; ``GET /stats``
reports what it saw. ``--deliver OPENAI_API_KEY=<value>`` pushes a funder's key to every
Runner, sealed, as the chart test's one API-key Directive needs.
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
    parser.add_argument(
        "--deliver",
        action="append",
        default=[],
        metavar="SLOT=VALUE",
        help="seal VALUE under SLOT to every Runner, for the fake's one Contract",
    )
    arguments = parser.parse_args()

    plane = FakeControlPlane(namespace=arguments.namespace, host_party=arguments.host_party)
    for delivery in arguments.deliver:
        slot, _, value = delivery.partition("=")
        plane.deliver(slot, value)
    server = plane.server(arguments.host, arguments.port)
    print(
        f"fake control plane on :{server.server_address[1]}, namespace {arguments.namespace}",
        flush=True,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
