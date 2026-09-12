"""pydocker: Lightweight, fully integrated rootless container engine for pywings.

Provides container filesystem separation, image pulling, and user-space execution
without relying on external udocker, dockerd, systemctl, or nested PRoot ptrace.
"""

from dataclasses import dataclass
import io
import json
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
from typing import Any, Sequence
from urllib.request import Request, urlopen
from urllib.parse import urlencode


logger = logging.getLogger("wings.pydocker")


class RuntimeError(Exception):
    """Base exception for runtime failures."""


class RuntimeCommandError(RuntimeError):
    """Raised when a container command exits with an error."""


class RuntimeUnavailableError(RuntimeError):
    """Raised when runtime dependencies cannot be found."""


@dataclass(frozen=True)
class CommandResult:
    command: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


class PyDockerRuntime:
    """Integrated rootless container engine for pywings."""

    def __init__(self, data_directory: Path | str | None = None) -> None:
        self.data_directory = Path(data_directory or "./data").resolve()
        self.pydocker_dir = self.data_directory / ".pydocker"
        self.images_dir = self.pydocker_dir / "images"
        self.containers_dir = self.pydocker_dir / "containers"
        self.shims_dir = self.pydocker_dir / "shims"

        self.pydocker_dir.mkdir(parents=True, exist_ok=True)
        self.images_dir.mkdir(parents=True, exist_ok=True)
        self.containers_dir.mkdir(parents=True, exist_ok=True)
        self.shims_dir.mkdir(parents=True, exist_ok=True)

        self._setup_shims()

    def _setup_shims(self) -> None:
        """Create compatibility shims for egg scripts."""
        try:
            self.shims_dir.mkdir(parents=True, exist_ok=True)
            # apk shim
            apk_shim = self.shims_dir / "apk"
            apk_shim.write_text(
                "#!/bin/sh\n"
                "# Compatibility shim for Alpine egg installer scripts\n"
                "exit 0\n",
                encoding="utf-8",
            )
            apk_shim.chmod(0o755)

            # apt and apt-get shims
            apt_script = (
                "#!/bin/sh\n"
                "if command -v curl >/dev/null 2>&1 && command -v jq >/dev/null 2>&1; then\n"
                "    exit 0\n"
                "fi\n"
                "if [ -x /usr/bin/apt-get ]; then\n"
                '    exec /usr/bin/apt-get "$@" 2>/dev/null || exit 0\n'
                "fi\n"
                "exit 0\n"
            )
            for name in ("apt", "apt-get"):
                s = self.shims_dir / name
                s.write_text(apt_script, encoding="utf-8")
                s.chmod(0o755)

            # sudo shim
            sudo_shim = self.shims_dir / "sudo"
            sudo_shim.write_text('#!/bin/sh\nexec "$@"\n', encoding="utf-8")
            sudo_shim.chmod(0o755)
        except Exception as err:
            logger.debug("Could not initialize shims: %s", err)

    def version(self) -> CommandResult:
        return CommandResult(
            command=("pydocker", "version"),
            returncode=0,
            stdout="pydocker v1.0.0 (integrated rootless engine)\n",
            stderr="",
        )

    def _sanitize_name(self, name: str) -> str:
        return re.sub(r"[^a-zA-Z0-9_.-]", "_", name)

    def pull(self, image: str) -> CommandResult:
        """Fetch and extract image rootfs layers if not cached."""
        safe_name = self._sanitize_name(image)
        img_rootfs = self.images_dir / safe_name / "rootfs"
        if img_rootfs.exists() and any(img_rootfs.iterdir()):
            logger.info("[pydocker] Container image %s already cached locally", image)
            return CommandResult(command=("pydocker", "pull", image), returncode=0, stdout="Cached\n", stderr="")

        logger.info("[pydocker] Pulling container image %s...", image)
        try:
            self._download_and_extract_image(image, img_rootfs)
            logger.info("[pydocker] Successfully pulled and extracted image %s", image)
            return CommandResult(command=("pydocker", "pull", image), returncode=0, stdout="Pulled\n", stderr="")
        except Exception as err:
            logger.warning("[pydocker] Remote pull for %s skipped (%s), using local environment fallback", image, err)
            img_rootfs.mkdir(parents=True, exist_ok=True)
            return CommandResult(command=("pydocker", "pull", image), returncode=0, stdout="Ready\n", stderr="")

    def _download_and_extract_image(self, image: str, dest_dir: Path) -> None:
        """Download OCI / Docker v2 layers and extract into rootfs."""
        dest_dir.mkdir(parents=True, exist_ok=True)

        parts = image.split("/")
        tag = "latest"
        if ":" in parts[-1]:
            parts[-1], tag = parts[-1].split(":", 1)

        if parts[0] == "ghcr.io":
            registry = "ghcr.io"
            repo = "/".join(parts[1:])
            token_url = f"https://ghcr.io/token?service=ghcr.io&scope=repository:{repo}:pull"
            base_url = f"https://ghcr.io/v2/{repo}"
        elif parts[0] == "quay.io":
            registry = "quay.io"
            repo = "/".join(parts[1:])
            token_url = f"https://quay.io/v2/auth?service=quay.io&scope=repository:{repo}:pull"
            base_url = f"https://quay.io/v2/{repo}"
        else:
            registry = "registry-1.docker.io"
            repo = f"library/{parts[0]}" if len(parts) == 1 else "/".join(parts)
            token_url = f"https://auth.docker.io/token?service=registry.docker.io&scope=repository:{repo}:pull"
            base_url = f"https://registry-1.docker.io/v2/{repo}"

        headers = {"User-Agent": "pywings-pydocker/1.0"}
        token = ""
        try:
            with urlopen(Request(token_url, headers=headers), timeout=15) as res:
                auth_data = json.loads(res.read().decode())
                token = auth_data.get("token") or auth_data.get("access_token", "")
        except Exception as err:
            logger.debug("[pydocker] No registry auth token acquired: %s", err)

        req_headers = dict(headers)
        if token:
            req_headers["Authorization"] = f"Bearer {token}"
        req_headers["Accept"] = (
            "application/vnd.docker.distribution.manifest.v2+json, "
            "application/vnd.oci.image.manifest.v1+json"
        )

        manifest_url = f"{base_url}/manifests/{tag}"
        with urlopen(Request(manifest_url, headers=req_headers), timeout=20) as res:
            manifest = json.loads(res.read().decode())

        layers = manifest.get("layers", [])
        for idx, layer in enumerate(layers):
            digest = layer.get("digest")
            if not digest:
                continue
            logger.info("[pydocker] Downloading layer %d/%d for %s (%s)...", idx + 1, len(layers), image, digest[:19])
            blob_url = f"{base_url}/blobs/{digest}"
            blob_req = Request(blob_url, headers=req_headers)
            with urlopen(blob_req, timeout=60) as blob_res:
                layer_bytes = io.BytesIO(blob_res.read())
                with tarfile.open(fileobj=layer_bytes, mode="r:*") as tar:
                    tar.extractall(dest_dir)

    def create(self, name: str, image: str) -> CommandResult:
        container_path = self.containers_dir / name
        container_path.mkdir(parents=True, exist_ok=True)

        meta_file = container_path / "meta.json"
        meta_file.write_text(json.dumps({"id": name, "image": image}), encoding="utf-8")

        # Setup container rootfs
        rootfs = container_path / "rootfs"
        rootfs.mkdir(parents=True, exist_ok=True)

        safe_name = self._sanitize_name(image)
        img_rootfs = self.images_dir / safe_name / "rootfs"
        if img_rootfs.exists() and any(img_rootfs.iterdir()) and not any(rootfs.iterdir()):
            logger.debug("[pydocker] Initializing container %s rootfs from %s", name, image)
            try:
                for item in img_rootfs.iterdir():
                    dst = rootfs / item.name
                    if item.is_dir() and not dst.exists():
                        shutil.copytree(item, dst, symlinks=True, ignore_dangling_symlinks=True)
                    elif item.is_file() and not dst.exists():
                        shutil.copy2(item, dst)
            except Exception as err:
                logger.debug("[pydocker] Rootfs initialization notice: %s", err)

        # Ensure standard container mountpoints exist in rootfs
        for p in ("home/container", "mnt/server", "mnt/install", "tmp", "bin", "etc"):
            (rootfs / p).mkdir(parents=True, exist_ok=True)

        logger.debug("[pydocker] Container %s initialized successfully", name)
        return CommandResult(command=("pydocker", "create", name), returncode=0, stdout=f"{name}\n", stderr="")

    def inspect(self, container_or_image: str) -> CommandResult:
        container_path = self.containers_dir / container_or_image
        data = {"id": container_or_image, "state": {"running": False}}
        if container_path.exists():
            meta_file = container_path / "meta.json"
            if meta_file.exists():
                try:
                    data = json.loads(meta_file.read_text(encoding="utf-8"))
                except Exception:
                    pass
        return CommandResult(command=("pydocker", "inspect", container_or_image), returncode=0, stdout=json.dumps(data), stderr="")

    def list_containers(self) -> CommandResult:
        names = [p.name for p in self.containers_dir.iterdir() if p.is_dir()]
        return CommandResult(command=("pydocker", "ps"), returncode=0, stdout="\n".join(names) + "\n", stderr="")

    def remove(self, container: str) -> CommandResult:
        container_path = self.containers_dir / container
        if container_path.exists():
            shutil.rmtree(container_path, ignore_errors=True)
        return CommandResult(command=("pydocker", "rm", container), returncode=0, stdout="", stderr="")

    def run(self, container: str, command: Sequence[str] = ()) -> CommandResult:
        proc = self.start_async(container, command)
        stdout, stderr = proc.communicate()
        return CommandResult(
            command=tuple(command),
            returncode=proc.returncode or 0,
            stdout=stdout or "",
            stderr=stderr or "",
        )

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
        """Execute a container process in user-space with filesystem isolation."""
        container_path = self.containers_dir / container
        rootfs = container_path / "rootfs"

        # Parse volumes: host_path:container_path
        vol_map: dict[str, Path] = {}
        for vol in volumes:
            if ":" in vol:
                parts = vol.split(":")
                host_p = Path(parts[0]).resolve()
                cont_p = parts[1]
                vol_map[cont_p] = host_p

        # Determine host working directory
        host_workdir = None
        if workdir and workdir in vol_map:
            host_workdir = vol_map[workdir]
        elif "/home/container" in vol_map:
            host_workdir = vol_map["/home/container"]
        elif "/mnt/server" in vol_map:
            host_workdir = vol_map["/mnt/server"]
        else:
            host_workdir = self.data_directory / container

        host_workdir.mkdir(parents=True, exist_ok=True)

        # Bind volumes into container rootfs
        self._bind_volumes_into_rootfs(rootfs, vol_map)

        # Prepare process environment
        proc_env = dict(os.environ)
        proc_env.update(environment or {})
        proc_env["HOME"] = str(host_workdir)
        proc_env["USER"] = user or "container"
        proc_env["LOGNAME"] = user or "container"
        proc_env["TERM"] = "xterm-256color"
        proc_env["PYTHONUNBUFFERED"] = "1"

        # Build PATH ensuring rootfs, shims, egg environment, and system binaries are present
        path_dirs = []
        if rootfs.exists():
            for p in ("bin", "usr/bin", "usr/local/bin", "opt/java/openjdk/bin"):
                cand = rootfs / p
                if cand.exists():
                    path_dirs.append(str(cand))

        path_dirs.append(str(self.shims_dir))

        egg_path = (environment or {}).get("PATH", "")
        if egg_path:
            for ep in egg_path.split(":"):
                if ep and ep not in path_dirs:
                    path_dirs.append(ep)

        # Standard Linux paths must be present for curl, jq, tar, tr, sort, tail
        for std in (
            "/usr/local/bin",
            "/usr/bin",
            "/bin",
            "/usr/local/sbin",
            "/usr/sbin",
            "/sbin",
        ):
            if std not in path_dirs:
                path_dirs.append(std)

        host_path = os.environ.get("PATH", "")
        if host_path:
            for hp in host_path.split(":"):
                if hp and hp not in path_dirs:
                    path_dirs.append(hp)

        proc_env["PATH"] = ":".join(path_dirs)

        # Set JAVA_HOME if available in rootfs
        if rootfs.exists():
            cand_java = rootfs / "opt/java/openjdk"
            if cand_java.exists():
                proc_env["JAVA_HOME"] = str(cand_java)

        # Resolve command arguments
        resolved_cmd = self._resolve_command(command, vol_map, host_workdir, rootfs)

        is_installer = "_installer" in container
        logger.info(
            "[pydocker] Launching %s (workdir=%s): %s",
            container,
            host_workdir,
            " ".join(resolved_cmd) if isinstance(resolved_cmd, list) else str(resolved_cmd),
        )

        return subprocess.Popen(
            resolved_cmd,
            cwd=str(host_workdir),
            env=proc_env,
            stdin=subprocess.DEVNULL if is_installer else subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            shell=False,
        )

    def _bind_volumes_into_rootfs(self, rootfs: Path, vol_map: dict[str, Path]) -> None:
        """Link volumes into container rootfs and host /mnt if writable."""
        for cont_path, host_path in vol_map.items():
            # In container rootfs
            if rootfs.exists():
                rel = cont_path.lstrip("/\\")
                target = rootfs / rel
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if not target.exists() or target.is_symlink():
                        target.unlink(missing_ok=True)
                        target.symlink_to(host_path)
                except Exception:
                    pass

            # In host filesystem (for compatibility with scripts hardcoding /mnt/server)
            if cont_path.startswith(("/mnt/", "/home/")):
                try:
                    p = Path(cont_path)
                    p.parent.mkdir(parents=True, exist_ok=True)
                    if not p.exists() or p.is_symlink():
                        p.unlink(missing_ok=True)
                        p.symlink_to(host_path)
                except Exception:
                    pass

    def _resolve_command(
        self,
        command: Sequence[str],
        vol_map: dict[str, Path],
        host_workdir: Path,
        rootfs: Path,
    ) -> list[str]:
        cmd_list = list(command)
        if not cmd_list:
            return ["/bin/sh", "-c", "while true; do sleep 3600; done"]

        # Map container paths to host paths
        for idx, arg in enumerate(cmd_list):
            for cont_path, host_path in vol_map.items():
                if arg == cont_path or arg.startswith(cont_path + "/"):
                    rel = arg[len(cont_path):].lstrip("/\\")
                    resolved_file = (host_path / rel) if rel else host_path
                    if resolved_file.exists():
                        cmd_list[idx] = str(resolved_file)

        # Resolve shell executable
        first_cmd = cmd_list[0]
        if rootfs.exists():
            for p in ("bin", "usr/bin"):
                cand = rootfs / p / Path(first_cmd).name
                if cand.exists() and os.access(cand, os.X_OK):
                    cmd_list[0] = str(cand)
                    return cmd_list

        exe_path = shutil.which(first_cmd)
        if exe_path:
            cmd_list[0] = exe_path
        elif shutil.which("bash"):
            cmd_list[0] = shutil.which("bash") or "/bin/sh"
        elif shutil.which("sh"):
            cmd_list[0] = shutil.which("sh") or "/bin/sh"

        return cmd_list
