"""CI partition gate for the two distributions (ADR-0013 §4, PRD issue 36).

The Runner is shipped to clients. What makes that safe is not obfuscation — it is that
the package *cannot* contain the platform: no service, no policy authority, no Evidence
sink, no API, no workflow. This walks the import graph of each distribution and fails the
build on the first module that imports anything but its own package, the standard library
and the distributions its ``pyproject.toml`` declares. So ``agentic_runner`` may import
``agentic_runner_contracts`` (declared) and contracts may import the Runner never (not
declared), and an undeclared third-party import fails here rather than in a client's venv.
"""

from __future__ import annotations

import ast
import sys
import textwrap
import tomllib
from collections.abc import Iterator
from importlib.metadata import packages_distributions
from pathlib import Path
from typing import Any

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

PACKAGES_ROOT = Path(__file__).resolve().parents[1] / "packages"

DISTRIBUTIONS = {
    "agentic_runner": PACKAGES_ROOT / "runner",
    "agentic_runner_contracts": PACKAGES_ROOT / "contracts",
}

_PLANTED_VIOLATION = """
    from platform_services.work_records import WorkRecordService


    def leak(service: WorkRecordService) -> None:
        del service
"""


def _project(project: Path) -> dict[str, Any]:
    pyproject = tomllib.loads((project / "pyproject.toml").read_text(encoding="utf-8"))
    table: dict[str, Any] = pyproject["project"]
    return table


def _name(project: Path) -> str:
    return canonicalize_name(_project(project)["name"])


def _declared(project: Path, extra: str | None = None) -> frozenset[str]:
    specs = _project(project)["dependencies"]
    if extra is not None:
        specs = [*specs, *_project(project)["optional-dependencies"][extra]]
    return frozenset(canonicalize_name(Requirement(spec).name) for spec in specs)


# The conformance kit's pytest half (runner-repo issue 07) may import the `testing` extra;
# nothing else may, the fake control plane included -- the chart and Docker tests serve it
# from the image, which installs no extra.
_TESTING_EXTRA_MODULES = ("agentic_runner/testing/plugin.py", "agentic_runner/testing/test_")


def _allowed_roots(package: str, declared: frozenset[str]) -> frozenset[str]:
    """Import names the distribution may reach: itself, and every top-level module that
    belongs to a declared distribution (``jwt`` for ``pyjwt``). The workspace members are
    editable installs, which ``packages_distributions`` cannot see, so they are mapped from
    their own pyproject."""

    roots = {package}
    for root, project in DISTRIBUTIONS.items():
        if _name(project) in declared:
            roots.add(root)
    for root, distributions in packages_distributions().items():
        if any(canonicalize_name(name) in declared for name in distributions):
            roots.add(root)
    return frozenset(roots)


def _modules(src: Path, package: str) -> Iterator[tuple[str, Path]]:
    for path in sorted((src / package).rglob("*.py")):
        yield path.relative_to(src).as_posix(), path


def _imported_roots(path: Path) -> set[str]:
    """Top-level package names ``path`` imports, relative imports resolved away."""

    roots: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                # A relative import never leaves its own distribution.
                continue
            if node.module is not None:
                roots.add(node.module.split(".", 1)[0])
    return roots


def _violations(src: Path, package: str, allowed: frozenset[str]) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for relative, path in _modules(src, package):
        outside = sorted(
            name
            for name in _imported_roots(path)
            if name not in allowed and name not in sys.stdlib_module_names
        )
        if outside:
            found[relative] = outside
    return found


@pytest.mark.parametrize("package", sorted(DISTRIBUTIONS))
def test_a_distribution_imports_only_itself_and_what_it_declares(package: str) -> None:
    project = DISTRIBUTIONS[package]
    declared = _declared(project)
    violations = _violations(project / "src", package, _allowed_roots(package, declared))
    if package == "agentic_runner":
        with_extra = _violations(
            project / "src", package, _allowed_roots(package, _declared(project, "testing"))
        )
        for module in [m for m in violations if m.startswith(_TESTING_EXTRA_MODULES)]:
            if module not in with_extra:
                del violations[module]

    assert violations == {}, (
        f"{package} may import only itself, the standard library and the distributions "
        f"its pyproject declares ({sorted(declared)}): {violations}"
    )


def test_the_contracts_package_may_not_import_the_runner() -> None:
    """The floor everyone installs depends on pydantic alone; the Runner is downstream."""

    declared = _declared(DISTRIBUTIONS["agentic_runner_contracts"])

    assert "agentic_runner" not in _allowed_roots("agentic_runner_contracts", declared)
    assert "agentic_runner_contracts" in _allowed_roots(
        "agentic_runner", _declared(DISTRIBUTIONS["agentic_runner"])
    )


def test_the_gate_fails_on_a_planted_platform_import(tmp_path: Path) -> None:
    """Test the test: a module inside ``agentic_runner`` that imports a module no declared
    distribution provides is a violation. Without this the walk could silently stop
    detecting anything."""

    planted = tmp_path / "agentic_runner" / "workers"
    planted.mkdir(parents=True)
    (planted / "planted.py").write_text(textwrap.dedent(_PLANTED_VIOLATION), encoding="utf-8")
    allowed = _allowed_roots("agentic_runner", _declared(DISTRIBUTIONS["agentic_runner"]))

    assert _violations(tmp_path, "agentic_runner", allowed) == {
        "agentic_runner/workers/planted.py": ["platform_services"]
    }
