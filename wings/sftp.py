"""Built-in SFTP subsystem matching Pterodactyl Wings SFTP server."""

import io
import logging
import os
from pathlib import Path
import re
import socket
from threading import Thread
import time
from typing import Any

import paramiko

from wings.remote import PanelRemoteClient, PanelRemoteError
from wings.servers import ServerStore


logger = logging.getLogger("wings.sftp")

# Usernames must match <user>.<server_identifier>
VALID_USERNAME_RE = re.compile(r"^(.+)\.([a-z0-9]{8})$", re.IGNORECASE)


class PteroSFTPHandle(paramiko.SFTPHandle):
    def __init__(self, flags: int, file_obj: Any) -> None:
        super().__init__(flags)
        self.file_obj = file_obj

    def stat(self) -> paramiko.SFTPAttributes:
        return paramiko.SFTPAttributes.from_stat(os.fstat(self.file_obj.fileno()))

    def chattr(self, attr: paramiko.SFTPAttributes) -> int:
        return paramiko.SFTP_OK

    def read(self, offset: int, length: int) -> bytes:
        self.file_obj.seek(offset)
        return self.file_obj.read(length)

    def write(self, offset: int, data: bytes) -> int:
        self.file_obj.seek(offset)
        self.file_obj.write(data)
        return paramiko.SFTP_OK

    def close(self) -> None:
        self.file_obj.close()


class PteroSFTPInterface(paramiko.SFTPServerInterface):
    def __init__(self, server_root: Path, permissions: list[str], server: Any = None) -> None:
        super().__init__(server)
        self.root = Path(server_root).resolve()
        self.permissions = set(permissions)

    def _resolve(self, path: str) -> Path:
        rel = path.lstrip("/\\")
        target = (self.root / rel).resolve()
        if target != self.root and self.root not in target.parents:
            raise PermissionError("Path resolves outside server root directory")
        return target

    def _has_perm(self, perm: str) -> bool:
        return "*" in self.permissions or perm in self.permissions

    def list_folder(self, path: str) -> list[paramiko.SFTPAttributes] | int:
        if not self._has_perm("file.read"):
            return paramiko.SFTP_PERMISSION_DENIED
        try:
            target = self._resolve(path)
            if not target.is_dir():
                return paramiko.SFTP_NO_SUCH_FILE
            entries = []
            for item in target.iterdir():
                try:
                    attr = paramiko.SFTPAttributes.from_stat(item.stat())
                    attr.filename = item.name
                    entries.append(attr)
                except OSError:
                    pass
            return entries
        except PermissionError:
            return paramiko.SFTP_PERMISSION_DENIED
        except OSError:
            return paramiko.SFTP_FAILURE

    def stat(self, path: str) -> paramiko.SFTPAttributes | int:
        try:
            target = self._resolve(path)
            return paramiko.SFTPAttributes.from_stat(target.stat())
        except FileNotFoundError:
            return paramiko.SFTP_NO_SUCH_FILE
        except PermissionError:
            return paramiko.SFTP_PERMISSION_DENIED
        except OSError:
            return paramiko.SFTP_FAILURE

    def lstat(self, path: str) -> paramiko.SFTPAttributes | int:
        try:
            target = self._resolve(path)
            return paramiko.SFTPAttributes.from_stat(target.lstat())
        except FileNotFoundError:
            return paramiko.SFTP_NO_SUCH_FILE
        except PermissionError:
            return paramiko.SFTP_PERMISSION_DENIED
        except OSError:
            return paramiko.SFTP_FAILURE

    def open(self, path: str, flags: int, attr: paramiko.SFTPAttributes) -> paramiko.SFTPHandle | int:
        try:
            target = self._resolve(path)
        except PermissionError:
            return paramiko.SFTP_PERMISSION_DENIED

        writing = bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC))
        if writing and not (self._has_perm("file.create") or self._has_perm("file.update")):
            return paramiko.SFTP_PERMISSION_DENIED
        if not writing and not self._has_perm("file.read-content"):
            return paramiko.SFTP_PERMISSION_DENIED

        mode = "rb"
        if (flags & os.O_RDWR) or (flags & os.O_WRONLY):
            if flags & os.O_CREAT:
                mode = "wb+" if (flags & os.O_TRUNC) else "rb+"
            else:
                mode = "rb+"

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists() and ("w" in mode or "+" in mode):
                target.touch()
            f = target.open(mode)
            return PteroSFTPHandle(flags, f)
        except FileNotFoundError:
            return paramiko.SFTP_NO_SUCH_FILE
        except OSError:
            return paramiko.SFTP_FAILURE

    def remove(self, path: str) -> int:
        if not self._has_perm("file.delete"):
            return paramiko.SFTP_PERMISSION_DENIED
        try:
            target = self._resolve(path)
            if target == self.root:
                return paramiko.SFTP_PERMISSION_DENIED
            target.unlink()
            return paramiko.SFTP_OK
        except FileNotFoundError:
            return paramiko.SFTP_NO_SUCH_FILE
        except OSError:
            return paramiko.SFTP_FAILURE

    def rename(self, oldpath: str, newpath: str) -> int:
        if not (self._has_perm("file.create") or self._has_perm("file.update")):
            return paramiko.SFTP_PERMISSION_DENIED
        try:
            src = self._resolve(oldpath)
            dst = self._resolve(newpath)
            if src == self.root or dst == self.root:
                return paramiko.SFTP_PERMISSION_DENIED
            dst.parent.mkdir(parents=True, exist_ok=True)
            src.rename(dst)
            return paramiko.SFTP_OK
        except FileNotFoundError:
            return paramiko.SFTP_NO_SUCH_FILE
        except OSError:
            return paramiko.SFTP_FAILURE

    def mkdir(self, path: str, attr: paramiko.SFTPAttributes) -> int:
        if not self._has_perm("file.create"):
            return paramiko.SFTP_PERMISSION_DENIED
        try:
            target = self._resolve(path)
            target.mkdir(parents=True, exist_ok=True)
            return paramiko.SFTP_OK
        except OSError:
            return paramiko.SFTP_FAILURE

    def rmdir(self, path: str) -> int:
        if not self._has_perm("file.delete"):
            return paramiko.SFTP_PERMISSION_DENIED
        try:
            target = self._resolve(path)
            if target == self.root:
                return paramiko.SFTP_PERMISSION_DENIED
            target.rmdir()
            return paramiko.SFTP_OK
        except FileNotFoundError:
            return paramiko.SFTP_NO_SUCH_FILE
        except OSError:
            return paramiko.SFTP_FAILURE

    def chattr(self, path: str, attr: paramiko.SFTPAttributes) -> int:
        return paramiko.SFTP_OK


class PteroSSHServer(paramiko.ServerInterface):
    def __init__(
        self,
        client_ip: str,
        remote_client: PanelRemoteClient,
        store: ServerStore,
    ) -> None:
        super().__init__()
        self.client_ip = client_ip
        self.remote_client = remote_client
        self.store = store
        self.auth_data: dict = {}

    def check_auth_password(self, username: str, password: str) -> int:
        if not VALID_USERNAME_RE.match(username):
            logger.warning("SFTP rejected invalid username format: %s", username)
            return paramiko.AUTH_FAILED

        try:
            auth_info = self.remote_client.validate_sftp_credentials(
                username=username,
                password=password,
                client_ip=self.client_ip,
            )
            server_uuid = auth_info.get("server")
            if not server_uuid or not self.store.get(server_uuid):
                logger.warning("SFTP authenticated user for unknown server: %s", server_uuid)
                return paramiko.AUTH_FAILED

            self.auth_data = auth_info
            logger.info("SFTP user %s authenticated successfully for server %s", username, server_uuid)
            return paramiko.AUTH_SUCCESSFUL
        except PanelRemoteError as err:
            logger.warning("SFTP authentication rejected by Panel for %s: %s", username, err)
            return paramiko.AUTH_FAILED
        except Exception as err:
            logger.error("SFTP authentication error: %s", err)
            return paramiko.AUTH_FAILED

    def check_channel_request(self, kind: str, chanid: int) -> int:
        if kind == "session":
            return paramiko.OPEN_SUCCEEDED
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED


class SFTPServer:
    """Manages listening socket and handles SFTP connections."""

    def __init__(
        self,
        host: str,
        port: int,
        data_directory: Path | str,
        remote_client: PanelRemoteClient,
        store: ServerStore,
    ) -> None:
        self.host = host
        self.port = port
        self.data_directory = Path(data_directory).resolve()
        self.remote_client = remote_client
        self.store = store
        self.host_key = self._load_or_generate_key()
        self._running = False
        self._sock: socket.socket | None = None

    def _load_or_generate_key(self) -> paramiko.PKey:
        key_dir = self.data_directory / ".sftp"
        key_dir.mkdir(parents=True, exist_ok=True)
        key_file = key_dir / "id_rsa"
        if key_file.exists():
            try:
                return paramiko.RSAKey.from_private_key_file(str(key_file))
            except Exception:
                pass
        key = paramiko.RSAKey.generate(2048)
        try:
            key.write_private_key_file(str(key_file))
        except Exception as err:
            logger.warning("Failed saving SFTP host key: %s", err)
        return key

    def start(self) -> None:
        """Start SFTP server listener thread."""
        Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        self._running = True
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self._sock.bind((self.host, self.port))
            self._sock.listen(100)
            logger.info("SFTP server listening on %s:%d", self.host, self.port)
        except Exception as err:
            logger.error("Failed to bind SFTP server on %s:%d: %s", self.host, self.port, err)
            return

        while self._running:
            try:
                client_sock, client_addr = self._sock.accept()
                Thread(target=self._handle_client, args=(client_sock, client_addr), daemon=True).start()
            except Exception:
                if not self._running:
                    break

    def _handle_client(self, client_sock: socket.socket, client_addr: tuple[str, int]) -> None:
        client_ip = client_addr[0]
        try:
            transport = paramiko.Transport(client_sock)
            transport.add_server_key(self.host_key)
            transport.set_subsystem_handler(
                "sftp",
                paramiko.SFTPServer,
                sftp_si=lambda: PteroSFTPInterface(
                    self.data_directory / ssh_server.auth_data["server"],
                    ssh_server.auth_data.get("permissions", []),
                ),
            )
            ssh_server = PteroSSHServer(client_ip, self.remote_client, self.store)
            transport.start_server(server=ssh_server)
            channel = transport.accept(20)
            if channel is None:
                transport.close()
                return
            while transport.is_active():
                time.sleep(1)
        except Exception as err:
            logger.debug("SFTP client %s disconnected: %s", client_ip, err)
        finally:
            try:
                client_sock.close()
            except Exception:
                pass
