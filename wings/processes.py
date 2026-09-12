"""udocker process lifecycle and state management matching Pterodactyl Wings."""

import ipaddress
import json
import logging
import os
from pathlib import Path
import re
import shlex
import shutil
from threading import Lock, RLock, Thread
import time
from typing import Any

from wings.events import (
    bus,
    ConsoleOutputEvent,
    DaemonMessageEvent,
    InstallCompletedEvent,
    InstallOutputEvent,
    InstallStartedEvent,
    StatsEvent,
    StatusEvent,
)
from wings.parser import ConfigParser
from wings.remote import PanelRemoteClient
from wings.runtime.pydocker import (
    PyDockerRuntime,
    RuntimeCommandError,
    RuntimeUnavailableError,
)
from wings.servers import ServerStore


logger = logging.getLogger("wings.processes")

STRIP_ANSI_REGEX = re.compile(
    r"[\u001B\u009B][\[\]()#;?]*(?:(?:(?:[a-zA-Z\d]*(?:;[a-zA-Z\d]*)*)?\u0007)|(?:(?:\d{1,4}(?:;\d{0,4})*)?[\dA-PRZcf-ntqry=><~]))"
)

STATE_OFFLINE = "offline"
STATE_STARTING = "starting"
STATE_RUNNING = "running"
STATE_STOPPING = "stopping"
STATE_INSTALLING = "installing"


class OutputLineMatcher:
    """Matches startup console lines using string or regex matching."""

    def __init__(self, raw: str) -> None:
        self.raw = str(raw)
        self.regex = None
        if self.raw.startswith("regex:") and len(self.raw) > 6:
            try:
                self.regex = re.compile(self.raw[6:])
            except re.error:
                self.regex = None

    def matches(self, line: str) -> bool:
        if self.regex is not None:
            return bool(self.regex.search(line))
        return self.raw in line


class ProcessManager:
    def __init__(
        self,
        store: ServerStore,
        runtime: PyDockerRuntime | Any,
        allowed_mounts=(),
        remote_client: PanelRemoteClient | None = None,
    ) -> None:
        self.store = store
        self.runtime = runtime
        self.allowed_mounts = tuple(Path(item).resolve() for item in allowed_mounts)
        self.remote_client = remote_client
        self._processes: dict[str, Any] = {}
        self._started_at: dict[str, float] = {}
        self._server_locks: dict[str, RLock] = {}
        self._last_crash: dict[str, float] = {}
        self._lock = RLock()

    def _get_server_lock(self, server_uuid: str) -> RLock:
        with self._lock:
            if server_uuid not in self._server_locks:
                self._server_locks[server_uuid] = RLock()
            return self._server_locks[server_uuid]

    def _log_path(self, server_uuid: str) -> Path:
        path = self.store.data_directory / server_uuid / "logs" / "console.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _install_log_path(self, server_uuid: str) -> Path:
        path = self.store.data_directory / server_uuid / "logs" / "install.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def set_server_state(self, server_uuid: str, state: str) -> None:
        """Update server state and publish to all listening websockets."""
        prev_server = self.store.get(server_uuid)
        prev_state = prev_server.state if prev_server else None
        self.store.update_state(server_uuid, state)

        if prev_state != state:
            logger.info("Server %s state transition: %s -> %s", server_uuid, prev_state, state)
            bus.publish(server_uuid, StatusEvent, state)
            if state == STATE_OFFLINE:
                bus.publish(
                    server_uuid,
                    StatsEvent,
                    json.dumps(self.stats(server_uuid), separators=(",", ":")),
                )

    def is_running(self, server_uuid: str) -> bool:
        with self._lock:
            process = self._processes.get(server_uuid)
            return process is not None and process.poll() is None

    def start(self, server_uuid: str, configuration: dict) -> None:
        lock = self._get_server_lock(server_uuid)
        with lock:
            server = self.store.get(server_uuid)
            current_state = server.state if server else STATE_OFFLINE
            if current_state == STATE_INSTALLING:
                logger.warning("Server %s is currently installing, cannot start", server_uuid)
                bus.publish(server_uuid, DaemonMessageEvent, "[Wings Daemon]: Server is currently installing, cannot start.")
                return

            if self.is_running(server_uuid) or current_state in {STATE_STARTING, STATE_RUNNING}:
                logger.info("Server %s is already starting or running; ignoring start request", server_uuid)
                return

            if current_state == STATE_STOPPING:
                logger.info("Server %s is currently stopping; waiting before restart", server_uuid)
                self._wait_for_offline(server_uuid, timeout=15)

            self._start_locked(server_uuid, configuration)

    def _start_locked(self, server_uuid: str, configuration: dict) -> None:
        self.set_server_state(server_uuid, STATE_STARTING)
        bus.publish(server_uuid, DaemonMessageEvent, "[Wings Daemon]: Preparing server environment for boot...")

        image = self.validate_configuration(configuration)
        environment = self._environment(configuration)
        self._apply_java_environment(image, environment)

        # Update process configuration files before booting
        server_root = (self.store.data_directory / server_uuid).resolve()
        server_root.mkdir(parents=True, exist_ok=True)
        try:
            ConfigParser(server_root, configuration, environment).update_configuration_files()
        except Exception as err:
            logger.warning("Failed updating config files for %s: %s", server_uuid, err)

        invocation = self._startup(configuration, environment)
        environment["STARTUP"] = invocation
        logger.info("Server %s booting with command: %s", server_uuid, invocation)
        command = ["/bin/sh", "-c", f"exec {invocation}"]
        volumes = self._volumes(server_uuid, configuration)
        publishes = []
        workdir, user = self._runtime_identity(configuration)
        entrypoint = self._entrypoint(configuration)
        command, entrypoint = self._adapt_reviactyl_entrypoint(image, command, entrypoint)

        try:
            self.runtime.pull(image)
        except RuntimeCommandError as error:
            logger.warning("Failed pulling image %s: %s", image, error)

        try:
            self.runtime.create(server_uuid, image)
        except RuntimeCommandError as error:
            msg = str(error).lower()
            if "already exists" not in msg and "already used" not in msg:
                raise

        process = self.runtime.start_async(
            server_uuid,
            command,
            environment=environment,
            volumes=volumes,
            publishes=publishes,
            workdir=workdir,
            user=user,
            entrypoint=entrypoint,
        )

        with self._lock:
            self._processes[server_uuid] = process
            self._started_at[server_uuid] = time.monotonic()

        bus.publish(server_uuid, DaemonMessageEvent, "[Wings Daemon]: Server process started.")
        Thread(target=self._watch, args=(server_uuid, process, configuration), daemon=True).start()
        Thread(
            target=self._enforce_limits,
            args=(server_uuid, process, configuration),
            daemon=True,
        ).start()

    def _wait_for_offline(self, server_uuid: str, timeout: int = 15) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                proc = self._processes.get(server_uuid)
            if proc is None or proc.poll() is not None:
                break
            time.sleep(0.5)

    def install(
        self,
        server_uuid: str,
        configuration: dict,
        reinstall: bool = False,
        start_on_completion: bool = False,
    ) -> None:
        """Run the server egg installation process in the background."""
        Thread(
            target=self._run_install,
            args=(server_uuid, configuration, reinstall, start_on_completion),
            daemon=True,
        ).start()

    def reinstall(self, server_uuid: str, configuration: dict) -> None:
        self.stop(server_uuid, configuration)
        try:
            self.runtime.remove(server_uuid)
        except (RuntimeCommandError, RuntimeUnavailableError):
            pass
        self.install(server_uuid, configuration, reinstall=True)

    def _run_install(
        self,
        server_uuid: str,
        configuration: dict,
        reinstall: bool = False,
        start_on_completion: bool = False,
    ) -> None:
        lock = self._get_server_lock(server_uuid)
        with lock:
            self.set_server_state(server_uuid, STATE_INSTALLING)
            bus.publish(server_uuid, InstallStartedEvent)
            bus.publish(server_uuid, DaemonMessageEvent, "[Wings Daemon]: Server installation started...")

            server_root = (self.store.data_directory / server_uuid).resolve()
            server_root.mkdir(parents=True, exist_ok=True)
            install_dir = server_root / ".install"
            install_dir.mkdir(parents=True, exist_ok=True)

            # Try to fetch fresh installation script from Panel
            script_text = ""
            container_image = ""
            entrypoint = ""

            if self.remote_client:
                try:
                    script_data = self.remote_client.get_installation_script(server_uuid)
                    if isinstance(script_data, dict):
                        script_text = script_data.get("script", "")
                        container_image = script_data.get("container_image", "")
                        entrypoint = script_data.get("entrypoint", "")
                except Exception as err:
                    logger.warning("Failed to fetch install script from panel for %s: %s", server_uuid, err)

            # Fallback to local configuration
            if not script_text:
                installation = configuration.get("installation") or {}
                if isinstance(installation, dict):
                    script_text = installation.get("script", "")
                    container_image = container_image or installation.get("container_image", "")
                    entrypoint = entrypoint or installation.get("entrypoint", "")
                elif isinstance(installation, str):
                    script_text = installation

            if not container_image:
                container_image = (
                    (configuration.get("container") or {}).get("image")
                    or configuration.get("image")
                    or "ghcr.io/pterodactyl/installers:alpine"
                )

            # Ensure configuration has all egg environment variables
            if self.remote_client and (not configuration.get("environment") or len(configuration.get("environment", {})) <= 2):
                try:
                    fresh_conf = self.remote_client.get_server_configuration(server_uuid)
                    if isinstance(fresh_conf, dict):
                        configuration.update(fresh_conf)
                        self.store.update_configuration(server_uuid, configuration)
                except Exception as err:
                    logger.warning("Could not refresh server configuration for %s: %s", server_uuid, err)

            # Create /mnt/server and /mnt/install symlinks on host if possible
            try:
                Path("/mnt").mkdir(parents=True, exist_ok=True)
                p_srv = Path("/mnt/server")
                if not p_srv.exists() or p_srv.is_symlink():
                    p_srv.unlink(missing_ok=True)
                    p_srv.symlink_to(server_root)
                p_inst = Path("/mnt/install")
                if not p_inst.exists() or p_inst.is_symlink():
                    p_inst.unlink(missing_ok=True)
                    p_inst.symlink_to(install_dir)
            except Exception:
                pass

            # Write installation script to disk with wrapper
            script_file = install_dir / "install.sh"
            normalized_script = (script_text or "").replace("\r\n", "\n")
            wrapper_header = (
                f'export SERVER_DIR="{server_root}"\n'
                'if [ ! -d /mnt/server ] && [ -d "$SERVER_DIR" ]; then\n'
                '    mkdir -p /mnt 2>/dev/null || true\n'
                '    ln -sfn "$SERVER_DIR" /mnt/server 2>/dev/null || true\n'
                'fi\n'
            )
            if normalized_script.startswith("#!"):
                lines = normalized_script.split("\n", 1)
                full_script = lines[0] + "\n" + wrapper_header + (lines[1] if len(lines) > 1 else "")
            else:
                full_script = "#!/bin/sh\n" + wrapper_header + normalized_script

            script_file.write_text(full_script, encoding="utf-8")
            try:
                script_file.chmod(0o755)
            except Exception:
                pass

            # In Pterodactyl installers, /mnt/server is the root and /mnt/install is the script folder
            volumes = [
                f"{server_root}:/mnt/server",
                f"{install_dir}:/mnt/install",
            ]
            installer_name = f"{server_uuid}_installer"

            try:
                self.runtime.remove(installer_name)
            except Exception:
                pass

            try:
                self.runtime.create(installer_name, container_image)
            except Exception:
                pass

            shell_exe = shutil.which(entrypoint) or shutil.which("bash") or shutil.which("ash") or "/bin/sh"
            command = [shell_exe, str(script_file)]

            try:
                process = self.runtime.start_async(
                    installer_name,
                    command,
                    environment=environment,
                    volumes=volumes,
                    workdir="/mnt/server",
                    entrypoint="",
                )

                # Close stdin immediately so the installer never hangs waiting for input
                if process.stdin is not None:
                    try:
                        process.stdin.close()
                    except Exception:
                        pass

                install_log = self._install_log_path(server_uuid)
                with install_log.open("a", encoding="utf-8", errors="replace") as log_f:
                    if process.stdout is not None:
                        for line in iter(process.stdout.readline, ""):
                            log_f.write(line)
                            log_f.flush()
                            clean = line.rstrip("\r\n")
                            if clean:
                                logger.info("[installer:%s] %s", server_uuid[:8], clean)
                                bus.publish(server_uuid, InstallOutputEvent, clean)
                                bus.publish(server_uuid, ConsoleOutputEvent, clean)

                exit_code = process.wait()
                successful = exit_code == 0
                logger.info("Installer process for %s exited with code %d (successful=%s)", server_uuid, exit_code, successful)
            except Exception as err:
                logger.error("Installation failed with error on server %s: %s", server_uuid, err)
                successful = False

            # Cleanup installer container and temp directory
            try:
                self.runtime.remove(installer_name)
            except Exception:
                pass
            shutil.rmtree(install_dir, ignore_errors=True)

            # CRITICAL: Notify Panel that install completed!
            if self.remote_client:
                Thread(
                    target=self._notify_install_with_retries,
                    args=(server_uuid, successful, reinstall),
                    daemon=True,
                ).start()

            self.set_server_state(server_uuid, STATE_OFFLINE if successful else "install_failed")
            bus.publish(server_uuid, InstallCompletedEvent)
            bus.publish(
                server_uuid,
                DaemonMessageEvent,
                f"[Wings Daemon]: Server installation completed (successful={successful}).",
            )

            if start_on_completion and successful:
                logger.info("Starting server %s after successful installation", server_uuid)
                self.start(server_uuid, configuration)

    def _notify_install_with_retries(self, server_uuid: str, successful: bool, reinstall: bool) -> None:
        """Retry sending installation status to Panel until acknowledged."""
        if not self.remote_client:
            return
        for attempt in range(1, 10):
            try:
                self.remote_client.set_installation_status(
                    server_uuid, successful=successful, reinstall=reinstall
                )
                logger.info("Successfully notified Panel of install status for %s", server_uuid)
                return
            except Exception as err:
                logger.warning(
                    "Attempt %d to notify Panel of install status for %s failed (%s), retrying in %ds...",
                    attempt,
                    server_uuid,
                    err,
                    min(30, attempt * 3),
                )
                time.sleep(min(30, attempt * 3))

    @staticmethod
    def validate_configuration(configuration: dict) -> str:
        if not isinstance(configuration, dict):
            raise ValueError("server configuration must be an object")
        image = (configuration.get("container") or {}).get("image") or configuration.get("image")
        if not isinstance(image, str) or not image.strip():
            raise ValueError("server configuration is missing container.image")
        limits = ProcessManager._limits(configuration)
        if not isinstance(limits, dict):
            raise ValueError("server configuration limits must be an object")
        for field in ("memory", "swap", "disk", "io"):
            if field in limits and limits[field] is not None:
                try:
                    if int(limits[field]) < 0:
                        raise ValueError
                except (TypeError, ValueError) as error:
                    raise ValueError(
                        f"server configuration limit {field} must be a non-negative integer"
                    ) from error
        if "cpu" in limits and limits["cpu"] is not None:
            try:
                if float(limits["cpu"]) < 0:
                    raise ValueError
            except (TypeError, ValueError) as error:
                raise ValueError("server configuration limit cpu must be non-negative") from error
        return image.strip()

    @staticmethod
    def _limits(configuration: dict) -> dict:
        limits = dict(configuration.get("limits") or {})
        build = configuration.get("build") or {}
        aliases = {
            "memory_limit": "memory",
            "disk_space": "disk",
            "cpu_limit": "cpu",
            "io_weight": "io",
        }
        for source, target in aliases.items():
            if target not in limits and build.get(source) is not None:
                limits[target] = build[source]
        return limits

    @staticmethod
    def _environment(configuration: dict) -> dict[str, str]:
        environment = {}
        container = configuration.get("container") or {}
        process = configuration.get("process_configuration") or {}
        for source in (
            process.get("environment") or {},
            container.get("environment") or {},
            configuration.get("environment") or {},
        ):
            if isinstance(source, dict):
                environment.update({str(key): str(value) for key, value in source.items()})
        environment.setdefault("SERVER_UUID", str(configuration.get("uuid", "")))
        ports = ProcessManager._publishes(configuration)
        if ports:
            parts = ports[0].split(":")
            primary_port = parts[1] if len(parts) == 3 else parts[0]
        else:
            primary_port = str(configuration.get("server_port", "25565"))
        environment.setdefault("SERVER_PORT", primary_port)
        return environment

    @staticmethod
    def _apply_java_environment(image: str, environment: dict[str, str]) -> None:
        image_name = image.lower()
        if "eclipse-temurin" in image_name or "reviactyl/images" in image_name:
            java_home = environment.setdefault("JAVA_HOME", "/opt/java/openjdk")
            current_path = environment.get("PATH", "")
            java_bin = f"{java_home}/bin"
            if java_bin not in current_path.split(":"):
                environment["PATH"] = f"{java_bin}:{current_path}" if current_path else java_bin

    @staticmethod
    def _publishes(configuration: dict) -> list[str]:
        raw = configuration.get("ports") or configuration.get("allocations") or []
        if isinstance(raw, dict):
            if raw.get("mappings") is not None:
                raw = raw["mappings"]
            elif raw.get("ports") is not None:
                raw = raw["ports"]
            elif raw.get("default") is not None:
                raw = [raw["default"]]
            else:
                raw = list(raw.values())
        if not isinstance(raw, list):
            if isinstance(raw, dict):
                expanded = []
                for address, container_ports in raw.items():
                    values = container_ports if isinstance(container_ports, list) else [container_ports]
                    expanded.extend(
                        {"ip": address, "host_port": value, "container_port": value}
                        for value in values
                    )
                raw = expanded
            else:
                raw = [raw]
        publishes = []
        for item in raw:
            if isinstance(item, dict):
                host = item.get("host_port", item.get("port"))
                container = item.get("container_port", item.get("port", host))
            else:
                host = container = item
            if isinstance(host, (list, tuple)):
                host = host[0] if host else None
            if isinstance(container, (list, tuple)):
                container = container[0] if container else host
            if isinstance(host, str) and ":" in host and container == host:
                pieces = host.split(":")
                if len(pieces) == 2:
                    host, container = pieces
                elif len(pieces) == 3:
                    _, host, container = pieces
            try:
                host = ProcessManager._port_number(host)
                container = ProcessManager._port_number(container)
            except (TypeError, ValueError) as error:
                raise ValueError("server port mappings must contain integers") from error
            if not 1 <= host <= 65535 or not 1 <= container <= 65535:
                raise ValueError("server ports must be between 1 and 65535")
            address = item.get("ip") if isinstance(item, dict) else None
            if address and str(address) not in {"0.0.0.0", "::", "*"}:
                try:
                    address = str(ipaddress.ip_address(str(address)))
                except ValueError as error:
                    raise ValueError("server allocation ip must be a valid IP address") from error
                publishes.append(f"{address}:{host}:{container}")
            else:
                publishes.append(f"{host}:{container}")
        return publishes

    @staticmethod
    def _port_number(value) -> int:
        match = re.search(r"\d+", str(value))
        if not match:
            raise ValueError("server port mappings must contain integers")
        return int(match.group(0))

    @staticmethod
    def _startup(configuration: dict, environment: dict[str, str]) -> str:
        container = configuration.get("container") or {}
        process = configuration.get("process_configuration") or {}
        service = configuration.get("service") or {}

        candidates = [
            configuration.get("invocation"),
            environment.get("STARTUP"),
            configuration.get("startup"),
            container.get("startup"),
            service.get("startup") if isinstance(service, dict) else None,
            process.get("invocation") if isinstance(process, dict) else None,
        ]
        startup = None
        for candidate in candidates:
            if isinstance(candidate, str) and candidate.strip():
                startup = candidate.strip()
                break

        if not startup:
            startup = "sh -c 'while true; do sleep 3600; done'"

        def replace(match):
            var_name = match.group(1)
            return str(environment.get(var_name, match.group(0)))

        return re.sub(r"\{\{([A-Za-z_][A-Za-z0-9_]*)\}\}", replace, startup)

    def _volumes(self, server_uuid: str, configuration: dict) -> list[str]:
        container = configuration.get("container") or {}
        mounts = configuration.get("mounts") or container.get("mounts") or []
        if not isinstance(mounts, list):
            raise ValueError("server configuration mounts must be an array")
        server_root = (self.store.data_directory / server_uuid).resolve()
        targets = {str(item.get("target") or item.get("destination")) for item in mounts if isinstance(item, dict)}
        volumes = [] if "/home/container" in targets else [f"{server_root}:/home/container"]
        for mount in mounts:
            if not isinstance(mount, dict):
                raise ValueError("each server mount must be an object")
            source = mount.get("source") or mount.get("src")
            target = mount.get("target") or mount.get("destination")
            if not source or not target or not str(target).startswith("/"):
                raise ValueError("each mount requires an absolute source and target")
            source_path = Path(str(source)).expanduser().resolve()
            allowed = source_path == server_root or server_root in source_path.parents
            allowed = allowed or any(item == source_path or item in source_path.parents for item in self.allowed_mounts)
            if not allowed:
                raise ValueError(f"mount source is not allowed: {source_path}")
            volumes.append(f"{source_path}:{target}")
        return volumes

    @staticmethod
    def _runtime_identity(configuration: dict) -> tuple[str, str | None]:
        container = configuration.get("container") or {}
        workdir = (
            configuration.get("working_directory")
            or configuration.get("workdir")
            or container.get("working_directory")
            or container.get("workdir")
            or "/home/container"
        )
        if not isinstance(workdir, str) or not workdir.startswith("/"):
            raise ValueError("working directory must be an absolute container path")
        user = configuration.get("user") or container.get("user")
        if user is not None and (not isinstance(user, str) or not user.strip()):
            raise ValueError("container user must be a non-empty string")
        return workdir, user.strip() if isinstance(user, str) else None

    @staticmethod
    def _entrypoint(configuration: dict) -> str | None:
        container = configuration.get("container") or {}
        # Empty entrypoint is intentional: udocker cannot always execute
        # Docker-specific helper scripts shipped by Pterodactyl images.
        entrypoint = configuration.get("entrypoint", container.get("entrypoint", ""))
        if entrypoint is None:
            return None
        if not isinstance(entrypoint, str):
            raise ValueError("container entrypoint must be a string")
        return entrypoint

    @staticmethod
    def _adapt_reviactyl_entrypoint(image: str, command: list[str], entrypoint: str | None):
        if entrypoint == "" and "reviactyl/images" in image:
            return ["/__cacert_entrypoint.sh", *command], "/bin/sh"
        return command, entrypoint

    def _watch(self, server_uuid: str, process, configuration: dict) -> None:
        log_path = self._log_path(server_uuid)
        process_config = configuration.get("process_configuration") or {}
        startup_config = process_config.get("startup") or {}
        done_entries = startup_config.get("done") or []
        strip_ansi = bool(startup_config.get("strip_ansi", False))

        matchers = [OutputLineMatcher(item) for item in done_entries if item]
        stop_config = process_config.get("stop") or {}
        stop_cmd = stop_config.get("value") if isinstance(stop_config, dict) else None

        with log_path.open("a", encoding="utf-8", errors="replace") as log_file:
            if process.stdout is not None:
                for line in process.stdout:
                    log_file.write(line)
                    log_file.flush()

                    stripped_line = line.rstrip("\r\n")
                    bus.publish(server_uuid, ConsoleOutputEvent, stripped_line)

                    # State transition check
                    server = self.store.get(server_uuid)
                    if server and server.state == STATE_STARTING:
                        clean_line = STRIP_ANSI_REGEX.sub("", stripped_line) if strip_ansi else stripped_line
                        is_done = False
                        if matchers:
                            for matcher in matchers:
                                if matcher.matches(clean_line):
                                    is_done = True
                                    break
                        else:
                            # If no done line matcher configured, mark running on first console activity
                            is_done = True

                        if is_done:
                            self.set_server_state(server_uuid, STATE_RUNNING)
                            bus.publish(server_uuid, DaemonMessageEvent, "[Wings Daemon]: Server marked as running.")

                    if stop_cmd and stripped_line.strip() == str(stop_cmd).strip():
                        self.set_server_state(server_uuid, STATE_STOPPING)

        exit_code = process.wait()
        with self._lock:
            self._processes.pop(server_uuid, None)
            self._started_at.pop(server_uuid, None)

        server = self.store.get(server_uuid)
        prev_state = server.state if server else STATE_OFFLINE
        self.set_server_state(server_uuid, STATE_OFFLINE)
        bus.publish(
            server_uuid,
            ConsoleOutputEvent,
            f"container@pterodactyl~ Server marked as offline (exit code {exit_code}).",
        )

        # Crash recovery matching Wings crash.go
        if prev_state in {STATE_STARTING, STATE_RUNNING} and exit_code != 0:
            crash_enabled = bool(configuration.get("crash_detection_enabled", True))
            if crash_enabled:
                now = time.monotonic()
                last_crash = self._last_crash.get(server_uuid, 0.0)
                self._last_crash[server_uuid] = now
                bus.publish(
                    server_uuid,
                    DaemonMessageEvent,
                    f"[Wings Daemon]: Server process crashed (exit code {exit_code})!",
                )
                if now - last_crash < 60:
                    bus.publish(
                        server_uuid,
                        DaemonMessageEvent,
                        "[Wings Daemon]: Server crashed too frequently, aborting automatic restart.",
                    )
                else:
                    bus.publish(
                        server_uuid,
                        DaemonMessageEvent,
                        "[Wings Daemon]: Attempting automatic restart in 5 seconds...",
                    )
                    Thread(target=self._auto_restart_worker, args=(server_uuid, configuration), daemon=True).start()

    def _auto_restart_worker(self, server_uuid: str, configuration: dict) -> None:
        time.sleep(5)
        server = self.store.get(server_uuid)
        if server and server.state == STATE_OFFLINE and not server.is_suspended:
            self.start(server_uuid, configuration)

    def _enforce_limits(self, server_uuid: str, process, configuration: dict) -> None:
        limits = self._limits(configuration)
        try:
            memory_limit = int(limits.get("memory", 0) or 0) * 1024 * 1024
            disk_limit = int(limits.get("disk", 0) or 0) * 1024 * 1024
            cpu_limit = float(limits.get("cpu", 0) or 0)
        except (TypeError, ValueError):
            return
        cpu_violations = 0
        while process.poll() is None:
            if memory_limit and self._linux_rss(getattr(process, "pid", None)) > memory_limit:
                self._limit_kill(server_uuid, process, "memory")
                return
            if disk_limit and self._disk_usage(server_uuid) > disk_limit:
                self._limit_kill(server_uuid, process, "disk")
                return
            if cpu_limit:
                if self._linux_cpu_percent(getattr(process, "pid", None)) > cpu_limit:
                    cpu_violations += 1
                else:
                    cpu_violations = 0
                if cpu_violations >= 3:
                    self._limit_kill(server_uuid, process, "cpu")
                    return
            time.sleep(2)

    def _limit_kill(self, server_uuid: str, process, resource: str) -> None:
        log_path = self._log_path(server_uuid)
        msg = f"[Wings] Server stopped: {resource} limit exceeded.\n"
        with log_path.open("a", encoding="utf-8") as log_file:
            log_file.write(msg)
        bus.publish(server_uuid, ConsoleOutputEvent, msg.strip())
        try:
            process.kill()
        except OSError:
            pass

    def stop(self, server_uuid: str, configuration: dict | None = None, wait_seconds: int = 30) -> None:
        lock = self._get_server_lock(server_uuid)
        with lock:
            with self._lock:
                process = self._processes.get(server_uuid)

            if process is None or process.poll() is not None:
                self.set_server_state(server_uuid, STATE_OFFLINE)
                return

            self.set_server_state(server_uuid, STATE_STOPPING)
            bus.publish(server_uuid, DaemonMessageEvent, "[Wings Daemon]: Stopping server instance...")

            stop_command = ((configuration or {}).get("process_configuration") or {}).get("stop")
            if isinstance(stop_command, dict):
                stop_value = stop_command.get("value")
            else:
                stop_value = stop_command or "stop"

            if stop_value and process.stdin is not None and process.poll() is None:
                try:
                    process.stdin.write(str(stop_value) + "\n")
                    process.stdin.flush()
                except (OSError, BrokenPipeError):
                    pass

            timeout = max(1, min(wait_seconds, 300))
            try:
                process.wait(timeout=timeout)
            except Exception:
                try:
                    process.terminate()
                    process.wait(timeout=5)
                except Exception:
                    try:
                        process.kill()
                    except Exception:
                        pass

            with self._lock:
                self._processes.pop(server_uuid, None)
                self._started_at.pop(server_uuid, None)

            self.set_server_state(server_uuid, STATE_OFFLINE)

    def kill(self, server_uuid: str) -> None:
        lock = self._get_server_lock(server_uuid)
        with lock:
            with self._lock:
                process = self._processes.get(server_uuid)

            if process is not None:
                try:
                    process.kill()
                except OSError:
                    pass

            with self._lock:
                self._processes.pop(server_uuid, None)
                self._started_at.pop(server_uuid, None)

            self.set_server_state(server_uuid, STATE_OFFLINE)

    def send_command(self, server_uuid: str, command: str) -> None:
        with self._lock:
            process = self._processes.get(server_uuid)
        if process is None or process.poll() is not None:
            raise RuntimeError("Cannot send commands to a stopped server instance.")
        if process.stdin is None:
            raise RuntimeError("The server process does not accept console input.")
        process.stdin.write(command + "\n")
        process.stdin.flush()

    def read_logs(self, server_uuid: str, size: int = 100) -> list[str]:
        size = max(1, min(size, 100))
        path = self._log_path(server_uuid)
        if not path.exists():
            return []
        return path.read_text(encoding="utf-8", errors="replace").splitlines()[-size:]

    def restart(self, server_uuid: str, configuration: dict, wait_seconds: int = 30) -> None:
        self.stop(server_uuid, configuration, wait_seconds)
        self.start(server_uuid, configuration)

    def remove(self, server_uuid: str) -> None:
        """Stop the managed process and remove its udocker container."""
        lock = self._get_server_lock(server_uuid)
        with lock:
            with self._lock:
                process = self._processes.get(server_uuid)
            if process is not None and process.poll() is None:
                process.kill()
                try:
                    process.wait(timeout=10)
                except Exception:
                    pass
            with self._lock:
                self._processes.pop(server_uuid, None)
                self._started_at.pop(server_uuid, None)
            try:
                self.runtime.remove(server_uuid)
            except (RuntimeUnavailableError, RuntimeCommandError):
                pass
            self.set_server_state(server_uuid, STATE_OFFLINE)

    def stats(self, server_uuid: str) -> dict:
        """Return Wings-shaped resource data for one managed process."""
        server = self.store.get(server_uuid)
        limits = self._limits(server.configuration) if server else {}
        memory_limit_mb = limits.get("memory", 0) or 0
        try:
            memory_limit = int(memory_limit_mb) * 1024 * 1024
        except (TypeError, ValueError):
            memory_limit = 0

        with self._lock:
            process = self._processes.get(server_uuid)
            started_at = self._started_at.get(server_uuid)

        current_state = server.state if server else STATE_OFFLINE
        if process is None or process.poll() is not None:
            return {
                "memory_bytes": 0,
                "memory_limit_bytes": memory_limit,
                "cpu_absolute": 0.0,
                "network": {"rx_bytes": 0, "tx_bytes": 0},
                "uptime": 0,
                "state": current_state if current_state != STATE_RUNNING else STATE_OFFLINE,
                "disk_bytes": self._disk_usage(server_uuid),
            }

        pid = getattr(process, "pid", None)
        memory_bytes = self._linux_rss(pid)
        cpu_absolute = self._linux_cpu_percent(pid)
        return {
            "memory_bytes": memory_bytes,
            "memory_limit_bytes": memory_limit,
            "cpu_absolute": cpu_absolute,
            "network": {"rx_bytes": 0, "tx_bytes": 0},
            "uptime": max(0, int(time.monotonic() - (started_at or time.monotonic()))),
            "state": current_state,
            "disk_bytes": self._disk_usage(server_uuid),
        }

    def _disk_usage(self, server_uuid: str) -> int:
        root = self.store.data_directory / server_uuid
        if not root.exists():
            return 0
        total = 0
        for path in root.rglob("*"):
            if path.is_file():
                try:
                    total += path.stat().st_size
                except OSError:
                    pass
        return total

    @staticmethod
    def _linux_rss(pid) -> int:
        if not pid or not Path("/proc").exists():
            return 0
        try:
            for line in Path(f"/proc/{pid}/status").read_text().splitlines():
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
        except (OSError, ValueError, IndexError):
            pass
        return 0

    @staticmethod
    def _linux_cpu_percent(pid) -> float:
        if not pid or not Path("/proc").exists():
            return 0.0
        try:
            fields = Path(f"/proc/{pid}/stat").read_text().split()
            process_ticks = int(fields[13]) + int(fields[14])
            clock_ticks = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
            uptime = float(Path("/proc/uptime").read_text().split()[0])
            start_ticks = int(fields[21])
            process_uptime = max(0.01, uptime - (start_ticks / clock_ticks))
            return round((process_ticks / clock_ticks) / process_uptime * 100, 2)
        except (OSError, ValueError, IndexError, KeyError, ZeroDivisionError):
            return 0.0
