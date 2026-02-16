from __future__ import annotations

from dataclasses import dataclass
from queue import Empty, Queue
from typing import Any


@dataclass
class WebhookEvent:
    symbol: str
    event: str
    pm_high: float
    gap_pct: float
    premarket_dollar_vol: float
    spread_pct: float
    ts: int
    raw: dict[str, Any]


class EventQueue:
    def __init__(self, maxsize: int = 5000) -> None:
        self._queue: Queue[WebhookEvent] = Queue(maxsize=maxsize)

    def put(self, event: WebhookEvent) -> None:
        self._queue.put(event, block=False)

    def get(self, timeout: float = 0.2) -> WebhookEvent | None:
        try:
            return self._queue.get(timeout=timeout)
        except Empty:
            return None

    def size(self) -> int:
        return self._queue.qsize()
