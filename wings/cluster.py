"""PyWings Cluster SFTP Client.

Connects automatically to the central SFTP router (server.py) over a TCP socket.
Eliminates the need for individual PyWings nodes to expose any public SFTP port.
"""

import json
import logging
import os
from pathlib import Path
import socket
import struct
import threading
import time
from typing import Any, Dict, Optional
import uuid

import paramiko

from wings.remote import PanelRemoteClient, PanelRemoteError
from wings.servers import ServerStore
from wings.sftp import PteroSFTPInterface, PteroSFTPHandle

logger = logging.getLogger("wings.cluster")

DEFAULT_ROUTER_HOST = "37.187.152.166"
DEFAULT_ROUTER_PORT = 2781


def send_frame(sock: socket.socket, meta: dict, payload: bytes = b"") -> None:
    meta_bytes = json.dumps(meta).encode("utf-8")
    meta_len = len(meta_bytes)
    payload_len = len(payload)
    total_len = 4 + meta_len + payload_len
    header = struct.pack("!II", total_len, meta_len)
    sock.sendall(header + meta_bytes + payload)


def recv_exact(sock: socket.socket, num_bytes: int) -> Optional[bytes]:
    buffer = bytearray()
    while len(buffer) < num_bytes:
        chunk = sock.recv(min(65536, num_bytes - len(buffer)))
        if not chunk:
            return None
        buffer.extend(chunk)
    return bytes(buffer)


def recv_frame(sock: socket.socket) -> tuple[Optional[dict], Optional[bytes]]:
    raw_head = recv_exact(sock, 8)
    if not raw_head:
        return None, None
    total_len, meta_len = struct.unpack("!II", raw_head)
    meta_bytes = recv_exact(sock, meta_len)
    if meta_bytes is None:
        return None, None
    payload_len = total_len - 4 - meta_len
    payload = recv_exact(sock, payload_len) if payload_len > 0 else b""
    if payload is None:
        return None, None
    meta = json.loads(meta_bytes.decode("utf-8"))
    return meta, payload


def sftp_attr_to_dict(attr: paramiko.SFTPAttributes) -> dict:
    return {
        "filename": getattr(attr, "filename", None),
        "st_size": getattr(attr, "st_size", 0),
        "st_uid": getattr(attr, "st_uid", 0),
        "st_gid": getattr(attr, "st_gid", 0),
        "st_mode": getattr(attr, "st_mode", 0),
        "st_atime": getattr(attr, "st_atime", 0),
        "st_mtime": getattr(attr, "st_mtime", 0),
    }


class ClusterSFTPClient:
    """Manages persistent connection from PyWings to the central SFTP server.py router."""

    def __init__(
        self,
        router_host: str = DEFAULT_ROUTER_HOST,
        router_port: int = DEFAULT_ROUTER_PORT,
        node_id: str = "",
        data_directory: Path | str = "./data",
        remote_client: Optional[PanelRemoteClient] = None,
        store: Optional[ServerStore] = None,
        activity_manager: Any = None,
    ):
        self.router_host = router_host
        self.router_port = int(router_port)
        self.node_id = node_id or socket.gethostname()
        self.data_directory = Path(data_directory).resolve()
        self.remote_client = remote_client
        self.store = store
        self.activity_manager = activity_manager

        self.sessions: Dict[str, PteroSFTPInterface] = {}
        self.handles: Dict[str, Dict[str, PteroSFTPHandle]] = {}
        self.send_lock = threading.Lock()
        self.running = False
        self.sock: Optional[socket.socket] = None
        self._worker_thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """Start the cluster socket client in a background thread."""
        self.running = True
        t = threading.Thread(target=self._run_loop, daemon=True, name="pywings-cluster-sftp")
        t.start()
        self._worker_thread = t

    def stop(self) -> None:
        """Stop the cluster socket client cleanly."""
        self.running = False
        sock = self.sock
        self.sock = None
        if sock:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                sock.close()
            except Exception:
                pass

    def _safe_send(self, meta: dict, payload: bytes = b"") -> None:
        with self.send_lock:
            if self.sock:
                try:
                    send_frame(self.sock, meta, payload)
                except Exception as err:
                    logger.debug("Error sending frame to router: %s", err)

    def _run_loop(self) -> None:
        logger.info(
            "Connecting PyWings node '%s' to Central SFTP Router at %s:%d...",
            self.node_id,
            self.router_host,
            self.router_port,
        )

        while self.running:
            try:
                self._connect_and_listen()
            except Exception as err:
                if self.running:
                    logger.warning(
                        "SFTP Cluster connection to %s:%d lost/failed (%s); reconnecting in 5s...",
                        self.router_host,
                        self.router_port,
                        err,
                    )
                    time.sleep(5)

    def _connect_and_listen(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        sock.connect((self.router_host, self.router_port))
        self.sock = sock

        # Handshake / Register node
        send_frame(sock, {
            "type": "register",
            "node_id": self.node_id,
        })

        ack, _ = recv_frame(sock)
        if not ack or ack.get("type") != "register_ack":
            raise ConnectionError(f"Router rejected node registration: {ack}")

        logger.info(
            "PyWings node '%s' REGISTERED with Central SFTP Router at %s:%d",
            self.node_id,
            self.router_host,
            self.router_port,
        )

        while self.running:
            meta, payload = recv_frame(sock)
            if meta is None:
                raise ConnectionResetError("Connection closed by central router")

            # Dispatch each command asynchronously to not block the socket reader
            threading.Thread(
                target=self._handle_command,
                args=(meta, payload or b""),
                daemon=True,
            ).start()

    def owns_server(self, short_id: str) -> Optional[str]:
        """Check if this node owns a server starting with short_id (8 chars)."""
        short_id = short_id.lower()
        if self.store:
            for s in self.store.all():
                if str(s.uuid).lower().startswith(short_id):
                    return s.uuid
        try:
            for p in self.data_directory.iterdir():
                if p.is_dir() and p.name.lower().startswith(short_id):
                    return p.name
        except Exception:
            pass
        return None

    def _handle_command(self, meta: dict, payload: bytes) -> None:
        msg_type = meta.get("type")
        req_id = meta.get("req_id")

        if msg_type == "check_auth":
            username = meta["username"]
            password = meta["password"]
            client_ip = meta.get("client_ip", "127.0.0.1")

            # Extract short server identifier from username (user.id8)
            parts = username.rsplit(".", 1)
            if len(parts) == 2 and len(parts[1]) == 8:
                short_id = parts[1].lower()
                # Fast check: if this node doesn't own this server, reject immediately (0ms)
                # This prevents all 13 nodes from spamming the Panel API simultaneously!
                if not self.owns_server(short_id):
                    self._safe_send({
                        "type": "auth_res",
                        "req_id": req_id,
                        "status": "not_mine",
                    })
                    return

            if not self.remote_client or not self.store:
                self._safe_send({
                    "type": "auth_res",
                    "req_id": req_id,
                    "status": "not_mine",
                })
                return

            try:
                logger.info("Server in %s belongs to this node (%s)! Validating credentials with Panel...", username, self.node_id)
                auth_info = self.remote_client.validate_sftp_credentials(
                    username=username,
                    password=password,
                    client_ip=client_ip,
                )
                server_uuid = auth_info.get("server")
                if not server_uuid:
                    self._safe_send({
                        "type": "auth_res",
                        "req_id": req_id,
                        "status": "not_mine",
                    })
                    return

                # Robust case-insensitive check in store or on disk
                matched_uuid = None
                srv = self.store.get(server_uuid) or self.store.get(str(server_uuid).lower()) or self.store.get(str(server_uuid).upper())
                if srv:
                    matched_uuid = srv.uuid
                else:
                    for s in self.store.all():
                        if str(s.uuid).lower() == str(server_uuid).lower():
                            matched_uuid = s.uuid
                            break

                if not matched_uuid:
                    try:
                        for p in self.data_directory.iterdir():
                            if p.is_dir() and p.name.lower() == str(server_uuid).lower():
                                matched_uuid = p.name
                                break
                    except Exception:
                        pass

                if matched_uuid:
                    auth_info["server"] = matched_uuid
                    logger.info("Cluster SFTP auth MATCHED for %s (server: %s) on node %s", username, matched_uuid, self.node_id)
                    self._safe_send({
                        "type": "auth_res",
                        "req_id": req_id,
                        "status": "ok",
                        "auth_info": auth_info,
                    })
                else:
                    logger.warning("Panel validated %s but server %s not found in store/disk on node %s", username, server_uuid, self.node_id)
                    self._safe_send({
                        "type": "auth_res",
                        "req_id": req_id,
                        "status": "not_mine",
                    })
            except PanelRemoteError as err:
                logger.debug("Cluster SFTP panel auth rejected %s: %s", username, err)
                self._safe_send({
                    "type": "auth_res",
                    "req_id": req_id,
                    "status": "failed",
                    "error": str(err),
                })
            except Exception as err:
                logger.warning("Cluster SFTP auth error for %s: %s", username, err)
                self._safe_send({
                    "type": "auth_res",
                    "req_id": req_id,
                    "status": "error",
                    "error": str(err),
                })

        elif msg_type == "session_init":
            session_id = meta["session_id"]
            auth_info = meta.get("auth_info", {})
            client_ip = meta.get("client_ip", "127.0.0.1")
            server_uuid = auth_info.get("server")
            permissions = auth_info.get("permissions", [])
            user_uuid = auth_info.get("user")

            server_root = self.data_directory / server_uuid
            ptero_iface = PteroSFTPInterface(
                server_root=server_root,
                permissions=permissions,
                server_uuid=server_uuid,
                user_uuid=user_uuid,
                client_ip=client_ip,
                activity_manager=self.activity_manager,
            )
            self.sessions[session_id] = ptero_iface
            self.handles[session_id] = {}
            self._safe_send({"type": "session_init_ack", "req_id": req_id, "status": "ok"})

        elif msg_type == "session_close":
            session_id = meta.get("session_id")
            if session_id:
                handles = self.handles.pop(session_id, {})
                for h in handles.values():
                    try:
                        h.close()
                    except Exception:
                        pass
                self.sessions.pop(session_id, None)
                self._safe_send({"type": "session_close_ack", "req_id": req_id, "status": "ok"})

        elif msg_type == "sftp_call":
            session_id = meta.get("session_id")
            iface = self.sessions.get(session_id)
            if not iface:
                self._safe_send({"req_id": req_id, "error_code": paramiko.SFTP_FAILURE})
                return

            op = meta.get("op")

            if op == "list_folder":
                path = meta.get("path", "/")
                res = iface.list_folder(path)
                if isinstance(res, int):
                    self._safe_send({"req_id": req_id, "error_code": res})
                else:
                    entries = [sftp_attr_to_dict(a) for a in res]
                    self._safe_send({"req_id": req_id, "status": "ok", "entries": entries})

            elif op in ("stat", "lstat"):
                path = meta.get("path", "/")
                fn = iface.stat if op == "stat" else iface.lstat
                res = fn(path)
                if isinstance(res, int):
                    self._safe_send({"req_id": req_id, "error_code": res})
                else:
                    self._safe_send({"req_id": req_id, "status": "ok", "stat": sftp_attr_to_dict(res)})

            elif op == "open":
                path = meta.get("path", "")
                flags = meta.get("flags", 0)
                dummy_attr = paramiko.SFTPAttributes()
                res = iface.open(path, flags, dummy_attr)
                if isinstance(res, int):
                    self._safe_send({"req_id": req_id, "error_code": res})
                else:
                    handle_id = uuid.uuid4().hex
                    self.handles.setdefault(session_id, {})[handle_id] = res
                    self._safe_send({"req_id": req_id, "status": "ok", "handle_id": handle_id})

            elif op == "fstat":
                handle_id = meta.get("handle_id")
                handle = self.handles.get(session_id, {}).get(handle_id)
                if handle:
                    st = handle.stat()
                    self._safe_send({"req_id": req_id, "status": "ok", "stat": sftp_attr_to_dict(st)})
                else:
                    self._safe_send({"req_id": req_id, "error_code": paramiko.SFTP_FAILURE})

            elif op == "read":
                handle_id = meta.get("handle_id")
                offset = int(meta.get("offset", 0))
                length = int(meta.get("length", 32768))
                handle = self.handles.get(session_id, {}).get(handle_id)
                if handle:
                    data = handle.read(offset, length)
                    self._safe_send({"req_id": req_id, "status": "ok"}, payload=data)
                else:
                    self._safe_send({"req_id": req_id, "error_code": paramiko.SFTP_FAILURE})

            elif op == "write":
                handle_id = meta.get("handle_id")
                offset = int(meta.get("offset", 0))
                handle = self.handles.get(session_id, {}).get(handle_id)
                if handle:
                    code = handle.write(offset, payload)
                    self._safe_send({"req_id": req_id, "status": "ok", "code": code})
                else:
                    self._safe_send({"req_id": req_id, "error_code": paramiko.SFTP_FAILURE})

            elif op == "close":
                handle_id = meta.get("handle_id")
                handle = self.handles.get(session_id, {}).pop(handle_id, None)
                if handle:
                    handle.close()
                self._safe_send({"req_id": req_id, "status": "ok"})

            elif op == "remove":
                path = meta.get("path", "")
                code = iface.remove(path)
                self._safe_send({"req_id": req_id, "code": code})

            elif op == "rename":
                oldpath = meta.get("oldpath", "")
                newpath = meta.get("newpath", "")
                code = iface.rename(oldpath, newpath)
                self._safe_send({"req_id": req_id, "code": code})

            elif op == "mkdir":
                path = meta.get("path", "")
                code = iface.mkdir(path, paramiko.SFTPAttributes())
                self._safe_send({"req_id": req_id, "code": code})

            elif op == "rmdir":
                path = meta.get("path", "")
                code = iface.rmdir(path)
                self._safe_send({"req_id": req_id, "code": code})
