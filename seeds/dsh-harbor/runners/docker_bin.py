"""Resolve the host docker CLI used to stage the dsh runtime image.

``DshAgent.install()`` extracts ``@deepseek-ai/dsh`` (and Node when the task
image is too old) from ``DSH_RUNTIME_IMAGE`` with ``docker create``/``cp``; the
doctor and the agent resolve the binary the same way. Policy: prefer
``DSH_DOCKER_BIN`` when set and executable, then ``PATH``; never hard-code
``/usr/bin/docker``.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Mapping


def resolve_docker_bin(*, env: Mapping[str, str] | None = None) -> str:
    """Return an executable docker path.

    Prefer ``DSH_DOCKER_BIN`` when set and executable; otherwise ``shutil.which``.
    Raise ``RuntimeError`` with a clear message when neither works.
    """
    source = os.environ if env is None else env
    explicit = (source.get("DSH_DOCKER_BIN") or "").strip()
    if explicit:
        if os.path.isfile(explicit) and os.access(explicit, os.X_OK):
            return explicit
        raise RuntimeError(f"DSH_DOCKER_BIN={explicit!r} is not an executable docker binary")
    found = shutil.which("docker", path=source.get("PATH"))
    if found:
        return found
    raise RuntimeError(
        "docker not found on PATH; install Docker or set DSH_DOCKER_BIN "
        "(Homebrew Mac: typically /opt/homebrew/bin/docker — do not assume /usr/bin/docker)"
    )
