"""Small, testable wrapper around the udocker command line interface."""

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import subprocess
from typing import Callable, Sequence


class RuntimeError(Exception):
    """Base exception for runtime failures."""


class RuntimeUnavailableError(RuntimeError):
    """Raised when udocker cannot be found on the host."""


class RuntimeCommandError(RuntimeError):
    """Raised when udocker exits with a non-zero status."""


@dataclass(frozen=True)
class CommandResult:
    """Normalized result of one udocker invocation."""

    command: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


Runner = Callable[..., subprocess.CompletedProcess[str]]


class UdockerRuntime:
    """Execute udocker commands without shell interpolation.

    The adapter does not require udocker to be installed during import. This
    keeps development and CI on Windows possible; availability is checked only
    when a real command is executed.
    """

    def __init__(
        self,
        executable: str | None = None,
        repository: Path | None = None,
        runner: Runner = subprocess.run,
    ) -> None:
        self.executable = executable or os.getenv("UDOCKER_BIN", "udocker")
        self.repository = repository
        self._runner = runner

    def _command(self, *args: str) -> list[str]:
        command = [self.executable]
        if self.repository is not None:
            command.append(f"--repo={self.repository}")
        command.extend(args)
        return command

    def execute(self, *args: str, check: bool = True) -> CommandResult:
        if shutil.which(self.executable) is None and Path(self.executable).name == self.executable:
            raise RuntimeUnavailableError(
                f"udocker was not found in PATH: {self.executable!r}. "
                "Install it on the Linux host before starting a server."
            )

        command = self._command(*args)
        result = self._runner(
            command,
            capture_output=True,
            text=True,
            check=False,
            shell=False,
        )
        normalized = CommandResult(
            command=tuple(command),
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )
        if check and result.returncode != 0:
            message = result.stderr.strip() or result.stdout.strip() or "unknown udocker error"
            raise RuntimeCommandError(f"udocker command failed ({result.returncode}): {message}")
        return normalized

    def version(self) -> CommandResult:
        return self.execute("version")

    def pull(self, image: str) -> CommandResult:
        return self.execute("pull", image)

    def create(self, name: str, image: str) -> CommandResult:
        return self.execute("create", f"--name={name}", image)

    def run(self, container: str, command: Sequence[str] = ()) -> CommandResult:
        return self.execute("run", container, *command)

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
        if shutil.which(self.executable) is None and Path(self.executable).name == self.executable:
            raise RuntimeUnavailableError(f"udocker was not found in PATH: {self.executable!r}")
        options = ["--nobanner"]
        for volume in volumes:
            options.append(f"--volume={volume}")
        for publish in publishes:
            options.append(f"--publish={publish}")
        for key, value in (environment or {}).items():
            options.append(f"--env={key}={value}")
        if workdir:
            options.append(f"--workdir={workdir}")
        if user:
            options.append(f"--user={user}")
        if entrypoint is not None:
            options.append(f"--entrypoint={entrypoint}")
        return subprocess.Popen(
            self._command("run", *options, container, *command),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            shell=False,
        )

    def inspect(self, container_or_image: str) -> CommandResult:
        return self.execute("inspect", container_or_image)

    def list_containers(self) -> CommandResult:
        return self.execute("ps", "-m", "-s")

    def remove(self, container: str) -> CommandResult:
        return self.execute("rm", container)
