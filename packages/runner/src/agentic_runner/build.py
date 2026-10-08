"""This build's own digest: the ``build_id`` a Runner reports and a release publishes.

Its own module, with no Runner import, because registration (the bootstrap request) and
the workstation (the heartbeat attestation) both need it and the workstation imports
registration. The release pipeline calls it on the installed wheel and on the image, so
any change to how it is computed changes every published ``build-id.txt`` with it.
"""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import agentic_runner

__all__ = ["build_id"]

_PACKAGE = Path(agentic_runner.__file__).parent


def build_id(package: Path = _PACKAGE) -> str:
    """The SHA-256 of the package's ``.py`` sources, path and bytes, in sorted order.

    Self-reported build identity (12 B7), not a signature: the consoles say
    "self-reported" beside it. Only ``.py`` files count, so a wheel, the image and a
    Homebrew install of one release give the same value whatever bytecode each compiled.
    """

    digest = sha256()
    for source in sorted(package.rglob("*.py")):
        digest.update(source.relative_to(package).as_posix().encode())
        digest.update(source.read_bytes())
    return digest.hexdigest()
