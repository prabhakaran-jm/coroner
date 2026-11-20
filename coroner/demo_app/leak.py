"""Thread-safe holder for deliberately retained bytes (Requirements 1.2, 1.3, 1.7, 1.8).

Design decisions:
- retain(n) returns the cumulative retained-byte total including this call.
- The chunk is allocated OUTSIDE the lock so a large fill never blocks readers.
  Only the append and the total update happen under the lock, and the total is
  updated strictly after the append succeeded. A failed allocation (MemoryError)
  or a failed append therefore leaves chunks and total unchanged.
- retain() validates only the type and sign of n (b"\\xa5" * -1 would silently be
  b""). Range bounds (1..104,857,600) and HTTP status codes belong to the endpoint.
"""

from __future__ import annotations

import threading


class LeakStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._chunks: list[bytes] = []
        self._total = 0

    @staticmethod
    def _allocate(n: int) -> bytes:
        # Multiplication writes every byte, so the pages are really touched.
        return b"\xa5" * n

    def retain(self, n: int) -> int:
        if isinstance(n, bool) or not isinstance(n, int):
            raise TypeError("n must be an int")
        if n < 0:
            raise ValueError("n must not be negative")
        chunk = self._allocate(n)  # MemoryError propagates; nothing changed yet
        with self._lock:
            self._chunks.append(chunk)  # if this raises, total is untouched
            self._total += n
            return self._total

    @property
    def total(self) -> int:
        with self._lock:
            return self._total

    @property
    def chunk_count(self) -> int:
        with self._lock:
            return len(self._chunks)
