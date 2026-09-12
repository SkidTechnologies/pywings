"""OCI / Docker image reference parser and normalizer."""

from dataclasses import dataclass
import re


DEFAULT_REGISTRY = "registry-1.docker.io"
DEFAULT_TAG = "latest"


@dataclass(frozen=True)
class OciReference:
    """Represents a fully-qualified or normalized OCI image reference."""

    registry: str
    repository: str
    tag: str | None = None
    digest: str | None = None

    @classmethod
    def parse(cls, raw: str) -> "OciReference":
        """Parse an image string into an OciReference object."""
        image = raw.strip()
        if not image:
            raise ValueError("Image reference cannot be empty")

        # Check for digest reference: repo@sha256:...
        digest = None
        tag = None
        if "@" in image:
            image, digest = image.split("@", 1)
            if not digest.startswith("sha256:"):
                raise ValueError(f"Invalid digest format: {digest}")
        elif ":" in image:
            # Check if colon is port in registry or tag
            # e.g., localhost:5000/repo:tag vs repo:tag
            last_slash = image.rfind("/")
            last_colon = image.rfind(":")
            if last_colon > last_slash:
                image, tag = image[:last_colon], image[last_colon + 1 :]

        if not tag and not digest:
            tag = DEFAULT_TAG

        # Split registry and repository
        parts = image.split("/")
        first = parts[0]

        # Determine if first component is a registry domain
        is_registry = "." in first or ":" in first or first in {"localhost", "docker.io"}

        if is_registry:
            registry = parts[0]
            repo_parts = parts[1:]
            if not repo_parts:
                raise ValueError(f"Invalid image reference missing repository: {raw}")
        else:
            registry = DEFAULT_REGISTRY
            repo_parts = parts

        # Normalize docker.io
        if registry in {"docker.io", "index.docker.io"}:
            registry = DEFAULT_REGISTRY

        # Docker Hub official library images (e.g. ubuntu -> library/ubuntu)
        if registry == DEFAULT_REGISTRY:
            if len(repo_parts) == 1:
                repository = f"library/{repo_parts[0]}"
            else:
                repository = "/".join(repo_parts)
        else:
            repository = "/".join(repo_parts)

        return cls(
            registry=registry,
            repository=repository,
            tag=tag,
            digest=digest,
        )

    @property
    def is_digest(self) -> bool:
        return self.digest is not None

    @property
    def normalized_name(self) -> str:
        """Return repo:tag or repo@digest without the registry host."""
        if self.digest:
            return f"{self.repository}@{self.digest}"
        return f"{self.repository}:{self.tag or DEFAULT_TAG}"

    @property
    def reference(self) -> str:
        """Return the tag or digest string used for manifest lookup."""
        return self.digest if self.digest else (self.tag or DEFAULT_TAG)

    def __str__(self) -> str:
        base = f"{self.registry}/{self.repository}"
        if self.digest:
            return f"{base}@{self.digest}"
        return f"{base}:{self.tag or DEFAULT_TAG}"
