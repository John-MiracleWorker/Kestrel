from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Collection, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import RLock

from ..models import MemoryHit, MemoryLayer, MemoryRecord


@dataclass(frozen=True)
class MemorySearchPage:
    """One bounded backend search page and its opaque continuation cursor."""

    hits: tuple[MemoryHit, ...]
    next_cursor: str | None = None


class MemoryBackend(ABC):
    """Backend interface for a single memory layer."""

    def __init__(self, path: Path, layer: MemoryLayer, **_: object) -> None:
        self.path = Path(path)
        self.layer = layer
        self._identity_reservation_lock = RLock()

    @abstractmethod
    def open(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def put(self, record: MemoryRecord) -> str:
        raise NotImplementedError

    @contextmanager
    def identity_reservation(self) -> Iterator[None]:
        """Serialize a cross-layer identity check through its first durable write.

        Backends with an existing operation lock should override this method so
        reservations also exclude writes outside the reservation protocol.
        """

        with self._identity_reservation_lock:
            yield

    def has_any_record_identity(self, record_ids: Collection[str]) -> bool:
        """Return whether any logical record or frame identity is already present.

        Built-in backends override this with an indexed lookup. The fallback
        preserves compatibility for custom backends implementing the older
        record lookup contract.
        """

        return any(
            self.get_record(record_id, include_inactive=True) is not None
            for record_id in record_ids
        )

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

    def find_page(
        self,
        query: str,
        k: int = 64,
        mode: str = "auto",
        min_relevancy: float = 0.0,
        *,
        include_inactive: bool = False,
        cursor: str | None = None,
    ) -> MemorySearchPage:
        """Return a bounded page for backends without native cursor support.

        The default implementation uses an opaque offset cursor and a one-hit
        lookahead. Backends with native cursors should override this method so
        later pages do not repeat ranking work.
        """

        offset = _offset_from_cursor(cursor)
        if k <= 0:
            return MemorySearchPage(hits=())
        end = offset + k
        window = self.find(
            query=query,
            k=end + 1,
            mode=mode,
            min_relevancy=min_relevancy,
            include_inactive=include_inactive,
        )
        return MemorySearchPage(
            hits=tuple(window[offset:end]),
            next_cursor=f"offset:{end}" if len(window) > end else None,
        )

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


def _offset_from_cursor(cursor: str | None) -> int:
    if cursor is None:
        return 0
    prefix = "offset:"
    if not cursor.startswith(prefix):
        raise ValueError("Invalid memory search cursor")
    try:
        offset = int(cursor.removeprefix(prefix))
    except ValueError as exc:
        raise ValueError("Invalid memory search cursor") from exc
    if offset < 0:
        raise ValueError("Invalid memory search cursor")
    return offset
