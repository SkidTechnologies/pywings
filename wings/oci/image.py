"""High-level OCI Image Manager coordinating pull, caching, and rootfs assembly."""

from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import shutil

from wings.oci.cache import ContentAddressableCache
from wings.oci.client import OciRegistryClient, OciRegistryError
from wings.oci.extractor import SafeLayerExtractor
from wings.oci.manifest import ImageConfig, Manifest, get_default_platform
from wings.oci.reference import OciReference


logger = logging.getLogger("wings.oci.image")


@dataclass
class ImageInstance:
    """Assembled OCI container image root filesystem and its configuration."""

    reference: OciReference
    manifest_digest: str
    config_digest: str
    config: ImageConfig
    rootfs: Path


class OciImageManager:
    """Manages downloading, caching, layer unpacking, and rootfs preparation for OCI images."""

    def __init__(
        self,
        cache_dir: Path | str,
        client: OciRegistryClient | None = None,
    ) -> None:
        self.cache = ContentAddressableCache(cache_dir)
        self.client = client or OciRegistryClient()

    def pull_image_sync(
        self,
        image_str: str,
        force_pull: bool = False,
    ) -> ImageInstance:
        """Fetch, verify, extract, and return the assembled OCI rootfs and config."""
        ref = OciReference.parse(image_str)
        logger.info("Pulling OCI image %s (%s)", image_str, ref)

        # 1. Fetch manifest (resolves multi-arch index / manifest list automatically)
        manifest, manifest_digest = self.client.get_manifest(ref)
        config_digest = manifest.config_descriptor.digest

        # Check if already cached and assembled with current safe extractor version
        version_marker = self.cache.get_rootfs_path(config_digest) / ".pywings_rootfs_v4"
        if not force_pull and self.cache.has_rootfs(config_digest) and version_marker.exists():
            cached_config_json = self.cache.get_config(config_digest)
            if cached_config_json:
                logger.info("Using cached assembled rootfs for image %s (%s)", ref, config_digest[:19])
                return ImageInstance(
                    reference=ref,
                    manifest_digest=manifest_digest,
                    config_digest=config_digest,
                    config=ImageConfig.from_dict(cached_config_json),
                    rootfs=self.cache.get_rootfs_path(config_digest),
                )

        # 2. Fetch image configuration JSON
        config_json = self.client.get_config_json(ref, config_digest)
        self.cache.save_config(config_digest, config_json)
        image_config = ImageConfig.from_dict(config_json)

        # 3. Download missing layer blobs into content-addressable storage
        layer_paths: list[Path] = []
        total_layers = len(manifest.layers)
        for idx, layer_desc in enumerate(manifest.layers):
            digest = layer_desc.digest
            blob_path = self.cache.get_blob_path(digest)
            if not self.cache.has_blob(digest):
                logger.info(
                    "Downloading layer %d/%d (%s, size=%d bytes)...",
                    idx + 1,
                    total_layers,
                    digest[:19],
                    layer_desc.size,
                )
                self.client.download_blob_to_file(ref, digest, str(blob_path))
            else:
                logger.debug("Reusing cached layer blob %s", digest[:19])
            layer_paths.append(blob_path)

        # 4. Assemble rootfs by applying layers in order with whiteouts
        rootfs_path = self.cache.get_rootfs_path(config_digest)
        if rootfs_path.exists():
            shutil.rmtree(rootfs_path, ignore_errors=True)
        rootfs_path.mkdir(parents=True, exist_ok=True)

        logger.info("Assembling rootfs for %s from %d layers...", ref, total_layers)
        extractor = SafeLayerExtractor(rootfs_path)
        extractor.extract_layers(layer_paths)

        # 5. Inject essential container configuration files
        self._inject_base_container_files(rootfs_path)
        (rootfs_path / ".pywings_rootfs_v4").touch(exist_ok=True)

        logger.info("Successfully assembled OCI rootfs at %s", rootfs_path)
        return ImageInstance(
            reference=ref,
            manifest_digest=manifest_digest,
            config_digest=config_digest,
            config=image_config,
            rootfs=rootfs_path,
        )

    def _inject_base_container_files(self, rootfs: Path) -> None:
        """Ensure /etc/resolv.conf, /etc/hosts, mount points, and permissions are valid."""
        etc = rootfs / "etc"
        etc.mkdir(parents=True, exist_ok=True)

        # resolv.conf (copy from host if exists, else public DNS)
        resolv_conf = etc / "resolv.conf"
        if resolv_conf.is_symlink():
            resolv_conf.unlink(missing_ok=True)
        if not resolv_conf.exists() or resolv_conf.stat().st_size == 0:
            if Path("/etc/resolv.conf").exists():
                try:
                    shutil.copy2("/etc/resolv.conf", resolv_conf)
                except Exception:
                    resolv_conf.write_text("nameserver 1.1.1.1\nnameserver 8.8.8.8\n", encoding="utf-8")
            else:
                resolv_conf.write_text("nameserver 1.1.1.1\nnameserver 8.8.8.8\n", encoding="utf-8")

        # hosts
        hosts = etc / "hosts"
        if hosts.is_symlink():
            hosts.unlink(missing_ok=True)
        if not hosts.exists():
            hosts.write_text("127.0.0.1 localhost\n::1 localhost\n", encoding="utf-8")

        # passwd and group
        passwd = etc / "passwd"
        if not passwd.exists() or passwd.stat().st_size == 0:
            passwd.write_text(
                "root:x:0:0:root:/root:/bin/sh\n"
                "container:x:1000:1000:container:/home/container:/bin/sh\n",
                encoding="utf-8",
            )

        group = etc / "group"
        if not group.exists() or group.stat().st_size == 0:
            group.write_text("root:x:0:\ncontainer:x:1000:\n", encoding="utf-8")

        # Standard container directories
        for d in ("home/container", "mnt/server", "mnt/install", "tmp", "dev", "proc", "sys"):
            (rootfs / d).mkdir(parents=True, exist_ok=True)
            try:
                (rootfs / d).chmod(0o777 if d == "tmp" else 0o755)
            except OSError:
                pass
