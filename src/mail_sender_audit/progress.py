"""Progress reporting protocol, so core logic never depends on a terminal library."""

from __future__ import annotations

from typing import Protocol


class Progress(Protocol):
    """Receives progress updates from long-running operations."""

    def start(self, description: str, total: int | None) -> None:
        """Begin a new phase. ``total`` is ``None`` when it is not known up front."""
        ...

    def advance(self, step: int = 1) -> None: ...


class NullProgress:
    """Progress sink that ignores everything (the default)."""

    def start(self, description: str, total: int | None) -> None:
        return None

    def advance(self, step: int = 1) -> None:
        return None
