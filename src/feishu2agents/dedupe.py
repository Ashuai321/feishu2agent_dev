"""Small in-process idempotency guard for retried Feishu events."""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum


class ProcessingState(Enum):
    PROCESSING = "processing"
    COMPLETED = "completed"


@dataclass
class _Entry:
    state: ProcessingState
    expires_at: float


class DedupeCache:
    def __init__(
        self,
        *,
        ttl_seconds: float = 600,
        max_entries: int = 10_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl_seconds <= 0 or max_entries <= 0:
            raise ValueError("ttl_seconds and max_entries must be positive")
        self._ttl_seconds = ttl_seconds
        self._max_entries = max_entries
        self._clock = clock
        self._entries: OrderedDict[str, _Entry] = OrderedDict()
        self._lock = threading.Lock()

    def begin(self, key: str) -> bool:
        """Reserve key for processing; return False when it is already active."""
        if not key:
            raise ValueError("dedupe key cannot be empty")
        with self._lock:
            now = self._clock()
            self._prune(now)
            if key in self._entries:
                return False
            while len(self._entries) >= self._max_entries:
                self._entries.popitem(last=False)
            self._entries[key] = _Entry(ProcessingState.PROCESSING, now + self._ttl_seconds)
            return True

    def complete(self, key: str) -> None:
        with self._lock:
            if key in self._entries:
                self._entries[key] = _Entry(
                    ProcessingState.COMPLETED, self._clock() + self._ttl_seconds
                )
                self._entries.move_to_end(key)

    def fail(self, key: str) -> None:
        with self._lock:
            self._entries.pop(key, None)

    def __len__(self) -> int:
        with self._lock:
            self._prune(self._clock())
            return len(self._entries)

    def _prune(self, now: float) -> None:
        expired = [key for key, entry in self._entries.items() if entry.expires_at <= now]
        for key in expired:
            del self._entries[key]
