"""OCI and Docker Image Manifest / Index parsing and platform resolution."""

from dataclasses import dataclass, field
import json
import logging
import platform


logger = logging.getLogger("wings.oci.manifest")

MEDIA_TYPE_DOCKER_MANIFEST_V2 = "application/vnd.docker.distribution.manifest.v2+json"
MEDIA_TYPE_DOCKER_MANIFEST_LIST = "application/vnd.docker.distribution.manifest.list.v2+json"
MEDIA_TYPE_OCI_MANIFEST_V1 = "application/vnd.oci.image.manifest.v1+json"
MEDIA_TYPE_OCI_INDEX_V1 = "application/vnd.oci.image.index.v1+json"

ALL_ACCEPTED_MANIFEST_TYPES = (
    f"{MEDIA_TYPE_DOCKER_MANIFEST_V2}, "
    f"{MEDIA_TYPE_OCI_MANIFEST_V1}, "
    f"{MEDIA_TYPE_DOCKER_MANIFEST_LIST}, "
    f"{MEDIA_TYPE_OCI_INDEX_V1}"
)


def get_default_platform() -> tuple[str, str]:
    """Return default (os, architecture) matching current host machine."""
    host_os = "linux"  # Containers always target Linux ABI in Pterodactyl
    machine = platform.machine().lower()
    arch_map = {
        "x86_64": "amd64",
        "amd64": "amd64",
        "aarch64": "arm64",
        "arm64": "arm64",
        "armv7l": "arm",
        "i386": "386",
        "i686": "386",
    }
    return host_os, arch_map.get(machine, "amd64")


@dataclass
class Descriptor:
    media_type: str
    digest: str
    size: int
    platform: dict[str, str] | None = None


@dataclass
class ImageConfig:
    env: list[str] = field(default_factory=list)
    cmd: list[str] = field(default_factory=list)
    entrypoint: list[str] = field(default_factory=list)
    working_dir: str = ""
    user: str = ""
    architecture: str = ""
    os: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "ImageConfig":
        config = data.get("config") or {}
        env = config.get("Env") or []
        cmd = config.get("Cmd") or []
        entrypoint = config.get("Entrypoint") or []
        working_dir = config.get("WorkingDir") or ""
        user = config.get("User") or ""
        architecture = data.get("architecture") or ""
        os_name = data.get("os") or ""
        return cls(
            env=list(env),
            cmd=list(cmd),
            entrypoint=list(entrypoint),
            working_dir=working_dir,
            user=user,
            architecture=architecture,
            os=os_name,
        )


@dataclass
class Manifest:
    schema_version: int
    media_type: str
    config_descriptor: Descriptor
    layers: list[Descriptor]


class ManifestParser:
    """Parses OCI Index, Docker Manifest List, and Image Manifests."""

    @staticmethod
    def is_index(raw_json: dict) -> bool:
        media_type = raw_json.get("mediaType", "")
        if media_type in {MEDIA_TYPE_DOCKER_MANIFEST_LIST, MEDIA_TYPE_OCI_INDEX_V1}:
            return True
        return "manifests" in raw_json and "layers" not in raw_json

    @staticmethod
    def resolve_platform_manifest(
        index_json: dict,
        target_os: str = "linux",
        target_arch: str = "amd64",
    ) -> Descriptor:
        """Select matching platform manifest from multi-arch index/manifest list."""
        manifests = index_json.get("manifests", [])
        if not manifests:
            raise ValueError("Manifest list/index contains no manifests")

        for item in manifests:
            plat = item.get("platform") or {}
            item_os = plat.get("os", "").lower()
            item_arch = plat.get("architecture", "").lower()
            if item_os == target_os and item_arch == target_arch:
                return Descriptor(
                    media_type=item.get("mediaType", MEDIA_TYPE_OCI_MANIFEST_V1),
                    digest=item["digest"],
                    size=int(item.get("size", 0)),
                    platform=plat,
                )

        # Fallback: find any linux architecture or first available
        for item in manifests:
            plat = item.get("platform") or {}
            if plat.get("os", "").lower() == "linux":
                return Descriptor(
                    media_type=item.get("mediaType", MEDIA_TYPE_OCI_MANIFEST_V1),
                    digest=item["digest"],
                    size=int(item.get("size", 0)),
                    platform=plat,
                )

        first = manifests[0]
        return Descriptor(
            media_type=first.get("mediaType", MEDIA_TYPE_OCI_MANIFEST_V1),
            digest=first["digest"],
            size=int(first.get("size", 0)),
            platform=first.get("platform"),
        )

    @staticmethod
    def parse_image_manifest(manifest_json: dict) -> Manifest:
        """Parse single-arch OCI/Docker image manifest."""
        schema_version = int(manifest_json.get("schemaVersion", 2))
        media_type = manifest_json.get("mediaType", MEDIA_TYPE_OCI_MANIFEST_V1)

        cfg = manifest_json.get("config")
        if not cfg or "digest" not in cfg:
            raise ValueError("Image manifest is missing valid config descriptor")

        config_desc = Descriptor(
            media_type=cfg.get("mediaType", ""),
            digest=cfg["digest"],
            size=int(cfg.get("size", 0)),
        )

        raw_layers = manifest_json.get("layers", [])
        layers = [
            Descriptor(
                media_type=item.get("mediaType", ""),
                digest=item["digest"],
                size=int(item.get("size", 0)),
            )
            for item in raw_layers
            if "digest" in item
        ]

        return Manifest(
            schema_version=schema_version,
            media_type=media_type,
            config_descriptor=config_desc,
            layers=layers,
        )

    # Aliases for convenience
    resolve_platform = resolve_platform_manifest
    parse = parse_image_manifest
