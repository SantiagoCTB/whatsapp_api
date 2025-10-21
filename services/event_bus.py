"""Simple in-memory broadcaster for server-sent events."""

from __future__ import annotations

import queue
import threading
from typing import Any, Dict


class EventBroadcaster:
    """Fan-out broadcaster backed by per-listener queues."""

    def __init__(self) -> None:
        self._listeners: set[queue.Queue] = set()
        self._lock = threading.Lock()

    def subscribe(self) -> queue.Queue:
        """Return a queue that receives future events."""
        subscriber: queue.Queue = queue.Queue(maxsize=16)
        with self._lock:
            self._listeners.add(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: queue.Queue) -> None:
        """Remove ``subscriber`` from the active listeners list."""
        with self._lock:
            self._listeners.discard(subscriber)

    def publish(self, event: Dict[str, Any] | None) -> None:
        """Publish ``event`` to all active subscribers."""
        if not event:
            return
        with self._lock:
            listeners = list(self._listeners)
        for subscriber in listeners:
            try:
                subscriber.put_nowait(event)
            except queue.Full:
                try:
                    subscriber.get_nowait()
                except queue.Empty:
                    pass
                try:
                    subscriber.put_nowait(event)
                except queue.Full:
                    # The listener is too slow; drop this event.
                    pass


message_broadcaster = EventBroadcaster()
"""Global broadcaster instance used across the application."""
