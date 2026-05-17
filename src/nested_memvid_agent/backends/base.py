from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from pathlib import Path

from ..models import MemoryHit, MemoryLayer, MemoryRecord


class MemoryBackend(ABC):
    """Backend interface for a single memory layer."""

    def __init__(self, path: Path, layer: MemoryLayer, **_: object) -> None:
        self.path = Path(path)
        self.layer = layer

    @abstractmethod
    def open(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def put(self, record: MemoryRecord) -> str:
        raise NotImplementedError

    @abstractmethod
    def find(
        self,
        query: str,
        k: int = 8,
        mode: str = "auto",
        min_relevancy: float = 0.0,
        *,
        include_inactive: bool = False,
    ) -> list[MemoryHit]:
        raise NotImplementedError

    @abstractmethod
    def upsert(self, record: MemoryRecord) -> str:
        raise NotImplementedError

    @abstractmethod
    def tombstone(self, record_id: str, *, reason: str, superseded_by: str | None = None) -> bool:
        raise NotImplementedError

    @abstractmethod
    def iter_records(self, *, include_inactive: bool = False) -> Iterable[MemoryRecord]:
        raise NotImplementedError

    @abstractmethod
    def get_record(self, record_id: str, *, include_inactive: bool = True) -> MemoryRecord | None:
        raise NotImplementedError

    @abstractmethod
    def seal(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def verify(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    def close(self) -> None:
        raise NotImplementedError
