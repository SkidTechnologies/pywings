"""Content-addressable storage cache for OCI layers, manifests, and rootfs."""

import json
import logging
import os
from pathlib import Path
import shutil
from typing import Any


logger = logging.getLogger("wings.oci.cache")


class ContentAddressableCache:
    """Manages local content-addressable storage for OCI blobs and assembled root filesystems."""

    def __init__(self, base_dir: Path | str) -> None:
        self.base_dir = Path(base_dir).resolve()
        self.blobs_dir = self.base_dir / "blobs" / "sha256"
        self.manifests_dir = self.base_dir / "manifests" / "sha256"
        self.configs_dir = self.base_dir / "configs" / "sha256"
        self.rootfs_dir = self.base_dir / "rootfs" / "sha256"

        self.blobs_dir.mkdir(parents=True, exist_ok=True)
        self.manifests_dir.mkdir(parents=True, exist_ok=True)
        self.configs_dir.mkdir(parents=True, exist_ok=True)
        self.rootfs_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _clean_digest(digest: str) -> str:
        """Strip sha256: prefix if present."""
        return digest.split(":", 1)[1] if ":" in digest else digest

    def get_blob_path(self, digest: str) -> Path:
        """Return the filesystem path for a layer/blob digest."""
        clean = self._clean_digest(digest)
        return self.blobs_dir / clean

    def has_blob(self, digest: str) -> bool:
        """Check if a layer blob is already present and non-empty in cache."""
        path = self.get_blob_path(digest)
        return path.is_file() and path.stat().st_size > 0

    def store_blob(self, digest: str, data: Any) -> Path:
        """Store blob content (stream or bytes) into cache."""
        target = self.get_blob_path(digest)
        tmp = target.with_suffix(".tmp")
        with tmp.open("wb") as f:
            if hasattr(data, "read"):
                shutil.copyfileobj(data, f)
            else:
                f.write(data)
        tmp.replace(target)
        return target

    def save_manifest(self, digest: str, data: dict) -> Path:
        """Save image manifest JSON keyed by digest."""
        clean = self._clean_digest(digest)
        target = self.manifests_dir / f"{clean}.json"
        target.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return target

    def get_manifest(self, digest: str) -> dict | None:
        """Retrieve cached manifest JSON if exists."""
        clean = self._clean_digest(digest)
        target = self.manifests_dir / f"{clean}.json"
        if target.is_file():
            try:
                return json.loads(target.read_text(encoding="utf-8"))
            except Exception:
                return None
        return None

    def save_config(self, digest: str, data: dict) -> Path:
        """Save container configuration JSON keyed by config digest."""
        clean = self._clean_digest(digest)
        target = self.configs_dir / f"{clean}.json"
        target.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return target

    def get_config(self, digest: str) -> dict | None:
        """Retrieve cached config JSON if exists."""
        clean = self._clean_digest(digest)
        target = self.configs_dir / f"{clean}.json"
        if target.is_file():
            try:
                return json.loads(target.read_text(encoding="utf-8"))
            except Exception:
                return None
        return None

    def get_rootfs_path(self, image_id: str) -> Path:
        """Return path to the assembled root filesystem for an image ID or config digest."""
        clean = self._clean_digest(image_id)
        return self.rootfs_dir / clean

    def has_rootfs(self, image_id: str) -> bool:
        """Check if assembled rootfs exists and is non-empty."""
        path = self.get_rootfs_path(image_id)
        return path.is_dir() and any(path.iterdir())

    def clear_rootfs(self, image_id: str) -> None:
        """Remove cached rootfs if it needs rebuilding."""
        path = self.get_rootfs_path(image_id)
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
