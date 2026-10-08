"""The Runner conformance kit (runner-repo issue 07, ``docs/conformance.md``).

A fake control plane (:class:`FakeControlPlane`) and a scenario suite that proves a Runner
build works without the platform: ``pip install 'agentic-runner[testing]'`` and then
``pytest --pyargs agentic_runner.testing``. The fake itself needs only the Runner's own
dependencies, so ``python -m agentic_runner.testing`` serves it from any Runner install,
the image included; pytest and the scenario fixtures come with the extra.
"""

from agentic_runner.testing.control_plane import (
    DEFAULT_NAMESPACE,
    RUNNER_PREFIX,
    Evidence,
    FakeControlPlane,
    Refusal,
)

__all__ = ["DEFAULT_NAMESPACE", "RUNNER_PREFIX", "Evidence", "FakeControlPlane", "Refusal"]
