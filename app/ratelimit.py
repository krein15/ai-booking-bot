"""Простые счётчики в памяти: защищают бесплатный пакет токенов от скриптов."""

from __future__ import annotations

import time
from collections import deque


class SlidingWindowLimiter:
    def __init__(self, clock=time.monotonic) -> None:
        self._clock = clock
        self._events: dict[str, deque[float]] = {}

    def hit(self, key: str, limit: int, window_seconds: float) -> bool:
        """Учитывает событие и возвращает True, если лимит не превышен."""
        if limit <= 0:
            return True
        now = self._clock()
        events = self._events.setdefault(key, deque())
        while events and now - events[0] >= window_seconds:
            events.popleft()
        if len(events) >= limit:
            return False
        events.append(now)
        return True

    def cleanup(self, max_window_seconds: float) -> None:
        now = self._clock()
        stale = [k for k, ev in self._events.items() if not ev or now - ev[-1] >= max_window_seconds]
        for key in stale:
            del self._events[key]
