"""The Agentic OS **Runner**: an activity-only executor, open source (ADR-0013).

Everything here runs where the workspace, the Agent Runtime subprocess, a verb seam or a
credential the Runner holds is — and nothing else. The Ralph Loop itself, every
``@workflow.defn``, the services, the policy authority, the Evidence sink and the API
stay on the platform; this package imports none of them, and a CI partition test
(``tests/test_package_partition.py``) fails the build if that changes.
"""

__version__ = "3.1.0"

__all__ = ["__version__"]
