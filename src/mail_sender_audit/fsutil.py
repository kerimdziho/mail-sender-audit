"""Small filesystem helpers."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def write_private_text(path: Path, text: str) -> None:
    """Atomically write ``text`` to ``path``, readable by the current user only.

    The data goes to a temp file in the same directory (created ``0600`` by
    ``mkstemp``, so it is never world-readable, not even briefly) and is then
    moved into place with ``os.replace``. Raises ``OSError`` on failure.
    """
    tmp_name: str | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}-", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp_name, path)
        tmp_name = None
    finally:
        if tmp_name is not None:
            Path(tmp_name).unlink(missing_ok=True)
