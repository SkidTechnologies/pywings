"""Persistent server registry used by the Wings API."""

from dataclasses import dataclass
import json
from pathlib import Path
from threading import RLock
from typing import Any
from uuid import UUID
from datetime import datetime, timezone
import mimetypes
import os


ZERO_UTILIZATION = {
    "cpu_absolute": 0.0,
    "memory_bytes": 0,
    "memory_limit_bytes": 0,
    "network_rx_bytes": 0,
    "network_tx_bytes": 0,
    "disk_bytes": 0,
    "uptime": 0,
}


def valid_server_uuid(value: str) -> bool:
    try:
        return UUID(value).version == 4
    except (ValueError, AttributeError):
        return False


@dataclass
class ServerRecord:
    uuid: str
    configuration: dict[str, Any]
    state: str = "offline"
    is_suspended: bool = False

    def to_api_response(self, utilization: dict[str, Any] | None = None) -> dict[str, Any]:
        return {
            "state": self.state,
            "is_suspended": self.is_suspended,
            "utilization": utilization or dict(ZERO_UTILIZATION),
            "configuration": self.configuration,
        }

    def to_storage(self) -> dict[str, Any]:
        return {
            "uuid": self.uuid,
            "configuration": self.configuration,
            "state": self.state,
            "is_suspended": self.is_suspended,
        }


class ServerStore:
    """Small JSON-backed registry, sufficient until the database stage."""

    def __init__(self, data_directory: str | Path) -> None:
        self.data_directory = Path(data_directory)
        self.path = self.data_directory / "servers.json"
        self._lock = RLock()
        self.data_directory.mkdir(parents=True, exist_ok=True)
        self._servers: dict[str, ServerRecord] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as registry:
            raw = json.load(registry)
        if not isinstance(raw, list):
            raise ValueError(f"server registry must contain a list: {self.path}")
        for item in raw:
            if not isinstance(item, dict) or not valid_server_uuid(str(item.get("uuid", ""))):
                continue
            self._servers[item["uuid"]] = ServerRecord(
                uuid=item["uuid"],
                configuration=item.get("configuration") or {"uuid": item["uuid"]},
                # A Python process cannot safely claim a previous child is
                # still managed after a Wings restart.
                state="offline",
                is_suspended=bool(item.get("is_suspended", False)),
            )

    def _save(self) -> None:
        temporary = self.path.with_suffix(".json.tmp")
        with temporary.open("w", encoding="utf-8") as registry:
            json.dump([server.to_storage() for server in self._servers.values()], registry, indent=2)
            registry.write("\n")
        temporary.replace(self.path)

    def all(self) -> list[ServerRecord]:
        with self._lock:
            return list(self._servers.values())

    def get(self, server_uuid: str) -> ServerRecord | None:
        with self._lock:
            return self._servers.get(server_uuid)

    def add(self, server: ServerRecord) -> None:
        with self._lock:
            self._servers[server.uuid] = server
            self._save()

    def ensure(self, server_uuid: str) -> ServerRecord:
        with self._lock:
            server = self._servers.get(server_uuid)
            if server is None:
                server = ServerRecord(uuid=server_uuid, configuration={"uuid": server_uuid})
                self._servers[server_uuid] = server
                self._save()
            return server

    def remove(self, server_uuid: str) -> bool:
        with self._lock:
            if server_uuid not in self._servers:
                return False
            del self._servers[server_uuid]
            self._save()
            return True

    def update_state(self, server_uuid: str, state: str) -> ServerRecord | None:
        with self._lock:
            server = self._servers.get(server_uuid)
            if server is None:
                return None
            server.state = state
            self._save()
            return server

    def update_configuration(self, server_uuid: str, configuration: dict[str, Any]) -> ServerRecord:
        with self._lock:
            server = self._servers.get(server_uuid)
            if server is None:
                server = ServerRecord(uuid=server_uuid, configuration=configuration)
                self._servers[server_uuid] = server
            else:
                server.configuration = configuration
            self._save()
            return server

    def list_directory(self, server_uuid: str, directory: str) -> list[dict[str, Any]]:
        root = (self.data_directory / server_uuid).resolve()
        root.mkdir(parents=True, exist_ok=True)
        relative = directory.lstrip("/\\")
        target = (root / relative).resolve()
        if target != root and root not in target.parents:
            raise ValueError("directory resolves outside the server data directory")
        if not target.is_dir():
            raise FileNotFoundError(directory)

        entries = []
        for entry in target.iterdir():
            stat = entry.stat()
            is_directory = entry.is_dir()
            entries.append({
                "name": entry.name,
                "created": datetime.fromtimestamp(stat.st_ctime, timezone.utc).isoformat().replace("+00:00", "Z"),
                "modified": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat().replace("+00:00", "Z"),
                "mode": "d---------" if is_directory else "----------",
                "mode_bits": format(stat.st_mode & 0o777, "o"),
                "size": 0 if is_directory else stat.st_size,
                "directory": is_directory,
                "file": not is_directory,
                "symlink": entry.is_symlink(),
                "mime": "inode/directory" if is_directory else (mimetypes.guess_type(entry.name)[0] or "application/octet-stream"),
            })
        return sorted(entries, key=lambda item: (not item["directory"], item["name"].lower()))
