# agentic-runner-contracts

The version floor between the Agentic OS control plane and an installable **Runner**
(ADR-0013 §4, §7): the activity I/O models both sides serialise, the control-plane
payload models a Runner activity parses (runtime context, Grant snapshot), and the
**Public Metadata** name builders that decide every Temporal-visible name (ADR-0010 §6).

It depends on `pydantic` and the standard library, and on nothing else — in particular
on no platform module. A CI partition test (`tests/test_package_partition.py`) fails the
build if that stops being true.

Licensed under AGPL-3.0-only.
