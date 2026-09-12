"""Abstract base container runtime interface for pywings."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence
import subprocess


class RuntimeError(Exception):
    """Base exception for runtime failures."""


class RuntimeCommandError(RuntimeError):
    """Raised when a container command exits with an error."""


class RuntimeUnavailableError(RuntimeError):
    """Raised when runtime dependencies (e.g. PRoot) cannot be found."""


@dataclass(frozen=True)
class CommandResult:
    command: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


class ContainerRuntime(ABC):
    """Abstract interface for container execution backends in pywings."""

    @abstractmethod
    def version(self) -> CommandResult:
        """Return runtime name and version string."""

    @abstractmethod
    def pull(self, image: str) -> CommandResult:
        """Pull and assemble the container rootfs for an image reference."""

    @abstractmethod
    def create(self, name: str, image: str) -> CommandResult:
        """Create a container instance directory associated with an image."""

    @abstractmethod
    def inspect(self, container_or_image: str) -> CommandResult:
        """Return metadata for an active or created container instance."""

    @abstractmethod
    def list_containers(self) -> CommandResult:
        """Return list of known containers."""

    @abstractmethod
    def remove(self, container: str) -> CommandResult:
        """Remove a container instance and release associated runtime resources."""

    @abstractmethod
    def start_async(
        self,
        container: str,
        command: Sequence[str] = (),
        *,
        environment: dict[str, str] | None = None,
        volumes: Sequence[str] = (),
        publishes: Sequence[str] = (),
        workdir: str | None = None,
        user: str | None = None,
        entrypoint: str | None = "",
    ) -> subprocess.Popen[str]:
        """Spawn an isolated container process returning the subprocess handle."""
