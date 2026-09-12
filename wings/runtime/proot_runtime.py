"""PRoot user-space container runtime with root emulation and process group isolation."""

from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import time
from typing import Any, Sequence

from wings.oci import ImageInstance, OciImageManager, OciReference
from wings.runtime.base import (
    CommandResult,
    ContainerRuntime,
    RuntimeCommandError,
    RuntimeError,
    RuntimeUnavailableError,
)
from wings.runtime.proot_detector import ProotDetector


logger = logging.getLogger("wings.runtime.proot")


class ProotRuntime(ContainerRuntime):
    """PRoot-powered userspace container runtime matching OCI/Docker specifications."""

    def __init__(
        self,
        data_directory: Path | str | None = None,
        proot_path: str | Path | None = None,
        oci_manager: OciImageManager | None = None,
    ) -> None:
        self.data_directory = Path(data_directory or "./data").resolve()
        self.containers_dir = self.data_directory / "containers"
        self.images_cache_dir = self.data_directory / "images"
        self.containers_dir.mkdir(parents=True, exist_ok=True)
        self.images_cache_dir.mkdir(parents=True, exist_ok=True)

        self.proot_path = str(proot_path) if proot_path else None
        self.oci_manager = oci_manager or OciImageManager(self.images_cache_dir)
        self._cached_images: dict[str, ImageInstance] = {}

    def version(self) -> CommandResult:
        """Return PRoot binary version and runtime identification."""
        try:
            proot_bin = ProotDetector.find_proot(self.proot_path)
            res = subprocess.run(
                [str(proot_bin), "--version"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            out = f"wings-proot-runtime 1.0.0 ({res.stdout.strip() or res.stderr.strip()})\n"
            return CommandResult(command=("proot", "--version"), returncode=0, stdout=out, stderr="")
        except RuntimeUnavailableError as err:
            return CommandResult(command=("proot", "--version"), returncode=1, stdout="", stderr=str(err))

    def pull(self, image: str) -> CommandResult:
        """Fetch OCI image layers and assemble rootfs."""
        logger.info("PRoot runtime pulling image: %s", image)
        try:
            instance = self.oci_manager.pull_image_sync(image)
            self._cached_images[image] = instance
            return CommandResult(
                command=("proot-oci", "pull", image),
                returncode=0,
                stdout=f"Image {image} successfully assembled at {instance.rootfs}\n",
                stderr="",
            )
        except Exception as err:
            logger.error("Failed pulling OCI image %s: %s", image, err)
            raise RuntimeCommandError(f"Failed pulling OCI image {image}: {err}") from err

    def create(self, name: str, image: str) -> CommandResult:
        """Create a container instance linked to the image rootfs."""
        container_path = self.containers_dir / name
        container_path.mkdir(parents=True, exist_ok=True)

        # Pull or get cached image
        instance = self._cached_images.get(image)
        if not instance:
            instance = self.oci_manager.pull_image_sync(image)
            self._cached_images[image] = instance

        # Write container metadata
        meta = {
            "id": name,
            "image": image,
            "rootfs": str(instance.rootfs),
            "config": {
                "env": instance.config.env,
                "cmd": instance.config.cmd,
                "entrypoint": instance.config.entrypoint,
                "working_dir": instance.config.working_dir,
                "user": instance.config.user,
            },
        }
        (container_path / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
        logger.info("Created container %s with rootfs %s", name, instance.rootfs)
        return CommandResult(command=("proot-oci", "create", name), returncode=0, stdout=f"{name}\n", stderr="")

    def inspect(self, container_or_image: str) -> CommandResult:
        """Return container instance metadata."""
        container_path = self.containers_dir / container_or_image
        data = {"id": container_or_image, "status": "unknown"}
        if container_path.exists():
            meta_file = container_path / "meta.json"
            if meta_file.exists():
                try:
                    data = json.loads(meta_file.read_text(encoding="utf-8"))
                except Exception:
                    pass
        return CommandResult(command=("proot-oci", "inspect", container_or_image), returncode=0, stdout=json.dumps(data), stderr="")

    def list_containers(self) -> CommandResult:
        names = [p.name for p in self.containers_dir.iterdir() if p.is_dir()]
        return CommandResult(command=("proot-oci", "ps"), returncode=0, stdout="\n".join(names) + "\n", stderr="")

    def remove(self, container: str) -> CommandResult:
        """Remove a container instance directory."""
        container_path = self.containers_dir / container
        if container_path.exists():
            shutil.rmtree(container_path, ignore_errors=True)
        return CommandResult(command=("proot-oci", "rm", container), returncode=0, stdout="", stderr="")

    def validate_safe_bindings(self, volumes: Sequence[str]) -> None:
        """Validate volume mounts to prevent host escapes or leaking host directories."""
        forbidden_roots = {"/", "/etc", "/root", "/var", "/bin", "/sbin", "/usr", "/lib", "/lib64", "/boot"}
        for vol in volumes:
            if ":" in vol:
                parts = vol.split(":")
                host_str = parts[0].strip()
                if host_str in forbidden_roots or host_str.rstrip("/\\") in forbidden_roots:
                    raise RuntimeError(f"Unsafe mount rejected: host path {host_str} is forbidden")
                try:
                    host_p = Path(host_str).resolve()
                    if host_p.as_posix() in forbidden_roots or str(host_p) in forbidden_roots:
                        raise RuntimeError(f"Unsafe mount rejected: host path {host_str} is forbidden")
                except Exception as err:
                    if isinstance(err, RuntimeError):
                        raise
                    raise RuntimeError(f"Invalid mount specification: {vol}") from err

    def build_proot_cmd(
        self,
        rootfs: Path | str,
        command: Sequence[str] = (),
        *,
        workdir: str | None = None,
        volumes: Sequence[str] = (),
    ) -> list[str]:
        """Construct the proot command arguments for execution in userspace sandbox."""
        self.validate_safe_bindings(volumes)
        proot_bin = self.proot_path or ProotDetector.find_proot()
        cont_cwd = workdir or "/home/container"
        rf_path = Path(rootfs).resolve()

        # Ensure fundamental container mount points and directories exist
        for d in ("dev", "proc", "sys", "tmp", "etc", "mnt"):
            try:
                (rf_path / d).mkdir(parents=True, exist_ok=True)
            except OSError:
                pass

        # Ensure DNS resolution files inside rootfs exist by copying host configs
        etc_dir = rf_path / "etc"
        for conf_file in ("resolv.conf", "hosts"):
            host_conf = Path(f"/etc/{conf_file}")
            guest_conf = etc_dir / conf_file
            if host_conf.is_file():
                try:
                    if guest_conf.is_symlink() or not guest_conf.exists():
                        guest_conf.unlink(missing_ok=True)
                        guest_conf.write_bytes(host_conf.read_bytes())
                except OSError:
                    pass

        cmd = [str(proot_bin)]
        if ProotDetector.supports_no_seccomp():
            cmd.append("-n")

        cmd.extend([
            "-0",   # Root emulation (UID 0 / GID 0 inside jail)
            "-r", str(rf_path),
            "-w", cont_cwd,
        ])

        # Bind system pseudo-filesystems safely if present
        for sys_mount in ("/dev", "/proc", "/sys"):
            if os.path.exists(sys_mount):
                cmd.extend(["-b", sys_mount])

        for vol in volumes:
            if ":" in vol:
                parts = vol.split(":")
                cont_target = parts[1].lstrip("/\\")
                try:
                    (rf_path / cont_target).mkdir(parents=True, exist_ok=True)
                except OSError:
                    pass
                cmd.extend(["-b", vol])

        exec_args = list(command) if command else ["/bin/sh", "-c", "while true; do sleep 3600; done"]
        cmd.extend(exec_args)
        return cmd

    def run(self, container: str, command: Sequence[str] = ()) -> CommandResult:
        proc = self.start_async(container, command)
        stdout, _ = proc.communicate()
        return CommandResult(
            command=tuple(command),
            returncode=proc.returncode or 0,
            stdout=stdout or "",
            stderr="",
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
        """Execute a process inside the PRoot jail with root emulation and process group isolation."""
        proot_bin = ProotDetector.find_proot(self.proot_path)

        # 1. Load container rootfs and image metadata
        container_path = self.containers_dir / container
        rootfs: Path | None = None
        img_env: list[str] = []

        if container_path.exists():
            meta_file = container_path / "meta.json"
            if meta_file.exists():
                try:
                    meta = json.loads(meta_file.read_text(encoding="utf-8"))
                    if meta.get("rootfs"):
                        rootfs = Path(meta["rootfs"]).resolve()
                    img_env = meta.get("config", {}).get("env", [])
                except Exception as err:
                    logger.debug("Failed reading container meta: %s", err)

        if not rootfs or not rootfs.exists():
            # Fallback to local default image rootfs or create placeholder
            rootfs = self.data_directory / "rootfs_default"
            rootfs.mkdir(parents=True, exist_ok=True)

        # 2. Parse volume bindings: host_path:container_path
        vol_map: dict[str, Path] = {}
        for vol in volumes:
            if ":" in vol:
                parts = vol.split(":")
                host_p = Path(parts[0]).resolve()
                cont_p = parts[1]
                vol_map[cont_p] = host_p
                # Ensure host directory exists
                host_p.mkdir(parents=True, exist_ok=True)

        # 3. Determine working directory inside container
        cont_cwd = workdir or "/home/container"
        host_cwd = vol_map.get(cont_cwd, self.data_directory / container)
        host_cwd.mkdir(parents=True, exist_ok=True)

        # Ensure container mount points exist inside rootfs
        for cont_p in vol_map:
            rel = cont_p.lstrip("/\\")
            (rootfs / rel).mkdir(parents=True, exist_ok=True)

        # 4. Construct PRoot command with root emulation and sandboxed mounts
        vol_list = [f"{host_p}:{cont_p}" for cont_p, host_p in vol_map.items()]
        proot_cmd = self.build_proot_cmd(
            rootfs=rootfs,
            command=command,
            workdir=cont_cwd,
            volumes=vol_list,
        )

        # 5. Environment variables
        # Start with a clean environment base, inject container image env, then server env
        proc_env: dict[str, str] = {
            "TERM": "xterm-256color",
            "HOME": "/home/container",
            "USER": "container",
            "LOGNAME": "container",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "PROOT_NO_SECCOMP": "1",    # Prevents ptrace seccomp collision in nested containers
            "PROOT_NO_SUBRECONF": "1",
            "GLIBC_TUNABLES": "glibc.pthread.rseq=0",  # Prevents glibc 2.35+ (Ubuntu 22/Debian 12) SIGSEGV under PRoot
            "PYTHONUNBUFFERED": "1",
            "PYTHONIOENCODING": "utf-8",
        }

        # Apply image config environment variables
        for item in img_env:
            if "=" in item:
                k, v = item.split("=", 1)
                proc_env[k] = v

        # Apply Pterodactyl server environment variables (highest precedence)
        if environment:
            proc_env.update(environment)

        # Merge and guarantee all PATH directories from server, image, and standard Linux dirs
        path_components: list[str] = []
        for src_path in (
            (environment or {}).get("PATH", ""),
            next((item.split("=", 1)[1] for item in img_env if item.startswith("PATH=")), ""),
            proc_env.get("PATH", ""),
        ):
            if src_path:
                for piece in src_path.split(":"):
                    if piece and piece not in path_components:
                        path_components.append(piece)

        for s_dir in ("/usr/local/sbin", "/usr/local/bin", "/usr/sbin", "/usr/bin", "/sbin", "/bin"):
            if s_dir not in path_components:
                path_components.append(s_dir)
        proc_env["PATH"] = ":".join(path_components)

        is_installer = "_installer" in container
        logger.info(
            "Spawning PRoot container %s [rootfs=%s, cwd=%s, root-id=enabled]: %s",
            container,
            rootfs.name,
            cont_cwd,
            " ".join(proot_cmd),
        )

        # 6. Process creation with process group isolation (start_new_session=True)
        # Creating a new session/process group ensures that all child processes
        # (e.g. Java JVM, Node.js, bash workers) can be killed atomically when the server stops.
        return subprocess.Popen(
            proot_cmd,
            cwd=str(host_cwd),
            env=proc_env,
            stdin=subprocess.DEVNULL if is_installer else subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,  # Isolates process group for clean tree termination
        )

    @staticmethod
    def terminate_process_tree(proc_or_pid: Any, force: bool = False, wait_seconds: int = 10) -> None:
        """Atomically terminate the entire process group spawned by PRoot."""
        if proc_or_pid is None:
            return

        proc: subprocess.Popen | None = None
        if isinstance(proc_or_pid, subprocess.Popen):
            if proc_or_pid.poll() is not None:
                return
            pid = proc_or_pid.pid
            proc = proc_or_pid
        elif isinstance(proc_or_pid, int):
            pid = proc_or_pid
        else:
            return

        logger.info("Terminating process tree for PID %d (process group, force=%s)", pid, force)

        # Attempt termination of the whole process group
        sig = signal.SIGKILL if force else signal.SIGTERM
        if hasattr(os, "killpg") and hasattr(os, "getpgid"):
            try:
                pgid = os.getpgid(pid)
                os.killpg(pgid, sig)
            except (ProcessLookupError, OSError):
                if proc is not None:
                    try:
                        proc.kill() if force else proc.terminate()
                    except OSError:
                        pass
        elif proc is not None:
            try:
                proc.kill() if force else proc.terminate()
            except OSError:
                pass

        if force or proc is None:
            return

        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                return
            time.sleep(0.2)

        # Forceful kill if still running
        logger.warning("Process group for PID %d did not stop gracefully; sending SIGKILL", pid)
        if hasattr(os, "killpg") and hasattr(os, "getpgid"):
            try:
                pgid = os.getpgid(pid)
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
        try:
            proc.kill()
            proc.wait(timeout=2)
        except Exception:
            pass
