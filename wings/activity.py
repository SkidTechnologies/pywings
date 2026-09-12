"""Activity event manager and periodic flusher to Pterodactyl Panel."""

from datetime import datetime, timezone
import logging
import threading
import time
from typing import Any


logger = logging.getLogger("wings.activity")


class ActivityManager:
    """Collects server activity events and flushes them periodically to the Panel."""

    def __init__(self, remote_client: Any, flush_interval: int = 20, max_batch: int = 50) -> None:
        self.remote_client = remote_client
        self.flush_interval = max(5, flush_interval)
        self.max_batch = max_batch
        self._queue: list[dict] = []
        self._lock = threading.Lock()
        self._running = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not self.remote_client or not getattr(self.remote_client, "base_url", None):
            return
        self._running = True
        self._thread = threading.Thread(target=self._flush_loop, daemon=True, name="pywings-activity-cron")
        self._thread.start()
        logger.debug("Activity cron started (flush_interval=%ds)", self.flush_interval)

    def stop(self) -> None:
        self._running = False
        self.flush()

    def record(
        self,
        server_uuid: str,
        event: str,
        metadata: dict | None = None,
        ip: str = "127.0.0.1",
    ) -> None:
        """Record an activity event for a server instance."""
        if not server_uuid or not event:
            return
        entry = {
            "server": server_uuid,
            "event": event,
            "metadata": metadata or {},
            "ip": ip or "127.0.0.1",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        with self._lock:
            self._queue.append(entry)
            if len(self._queue) > 500:
                self._queue = self._queue[-500:]

    def flush(self) -> None:
        """Send all queued activity events to the Panel in batches."""
        with self._lock:
            if not self._queue:
                return
            batch = self._queue[:self.max_batch]
            self._queue = self._queue[len(batch):]

        try:
            self.remote_client.send_activity_logs(batch)
        except Exception as err:
            logger.debug("Could not flush activity logs to Panel: %s", err)

    def _flush_loop(self) -> None:
        while self._running:
            time.sleep(self.flush_interval)
            try:
                self.flush()
            except Exception:
                pass
