"""Event bus and subscription manager for server events and websocket dispatch."""

from collections import defaultdict
import json
import logging
from queue import Queue, Empty
from threading import RLock
from typing import Any, Callable


logger = logging.getLogger("wings.events")

# Event constants matching Pterodactyl Wings
DaemonMessageEvent = "daemon message"
InstallOutputEvent = "install output"
InstallStartedEvent = "install started"
InstallCompletedEvent = "install completed"
ConsoleOutputEvent = "console output"
StatusEvent = "status"
StatsEvent = "stats"
BackupRestoreCompletedEvent = "backup restore completed"
BackupCompletedEvent = "backup completed"
TransferLogsEvent = "transfer logs"
TransferStatusEvent = "transfer status"
DeletedEvent = "deleted"
TokenExpiringEvent = "token expiring"
TokenExpiredEvent = "token expired"


class ServerEventBus:
    """Thread-safe publish/subscribe bus scoped by server UUID."""

    def __init__(self) -> None:
        self._listeners: dict[str, set[Queue]] = defaultdict(set)
        self._callbacks: dict[str, set[Callable[[str, Any], None]]] = defaultdict(set)
        self._lock = RLock()

    def subscribe(self, server_uuid: str, maxsize: int = 256) -> Queue:
        """Subscribe a queue to receive events for a server."""
        q: Queue = Queue(maxsize=maxsize)
        with self._lock:
            self._listeners[server_uuid].add(q)
        return q

    def unsubscribe(self, server_uuid: str, q: Queue) -> None:
        """Remove a subscribed queue."""
        with self._lock:
            self._listeners[server_uuid].discard(q)
            if not self._listeners[server_uuid]:
                self._listeners.pop(server_uuid, None)

    def add_callback(self, server_uuid: str, callback: Callable[[str, Any], None]) -> None:
        """Register a callback function for server events."""
        with self._lock:
            self._callbacks[server_uuid].add(callback)

    def remove_callback(self, server_uuid: str, callback: Callable[[str, Any], None]) -> None:
        """Remove a registered callback."""
        with self._lock:
            self._callbacks[server_uuid].discard(callback)
            if not self._callbacks[server_uuid]:
                self._callbacks.pop(server_uuid, None)

    def publish(self, server_uuid: str, event: str, args: list[Any] | str | None = None) -> None:
        """Broadcast an event to all subscribed queues and callbacks for a server."""
        if args is None:
            formatted_args = []
        elif isinstance(args, list):
            formatted_args = [str(a) for a in args]
        else:
            formatted_args = [str(args)]

        message = {"event": event, "args": formatted_args}

        with self._lock:
            listeners = list(self._listeners.get(server_uuid, ()))
            callbacks = list(self._callbacks.get(server_uuid, ()))

        for q in listeners:
            try:
                q.put_nowait(message)
            except Exception:
                # If queue is full, drop oldest item and put new message
                try:
                    q.get_nowait()
                    q.put_nowait(message)
                except Exception:
                    pass

        for cb in callbacks:
            try:
                cb(event, formatted_args)
            except Exception as err:
                logger.warning("Error in event callback for server %s: %s", server_uuid, err)


# Global event bus singleton instance
bus = ServerEventBus()
