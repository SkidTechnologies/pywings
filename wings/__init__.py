"""PyWings Game Tunnel Client.

Maintains persistent reverse tunnel to the central Minecraft router (37.187.152.166:2782).
Allows game servers to run purely locally on 127.0.0.1 without exposing ANY open ports to the internet.
Supports auto-resolving server ports on demand.
"""

import logging
import select
import socket
import threading
import time
from typing import Any, Optional

logger = logging.getLogger("wings.tunnel")

DEFAULT_ROUTER_HOST = "37.187.152.166"
DEFAULT_TUNNEL_PORT = 2782


def bridge_sockets(s1: socket.socket, s2: socket.socket) -> None:
    try:
        while True:
            r, _, _ = select.select([s1, s2], [], [], 60)
            if not r:
                break
            if s1 in r:
                data = s1.recv(65536)
                if not data:
                    break
                s2.sendall(data)
            if s2 in r:
                data = s2.recv(65536)
                if not data:
                    break
                s1.sendall(data)
    except Exception:
        pass
    finally:
        try:
            s1.close()
        except Exception:
            pass
        try:
            s2.close()
        except Exception:
            pass


class GameTunnelClient:
    """Connects to central router tunnel port and handles local socket bridging."""

    def __init__(
        self,
        router_host: str = DEFAULT_ROUTER_HOST,
        router_port: int = DEFAULT_TUNNEL_PORT,
        node_id: str = "",
        store: Any = None,
    ):
        self.router_host = router_host
        self.router_port = int(router_port)
        self.node_id = node_id or socket.gethostname()
        self.store = store
        self.running = False
        self.control_sock: Optional[socket.socket] = None

    def start(self) -> None:
        self.running = True
        t = threading.Thread(target=self._run_loop, daemon=True, name="pywings-game-tunnel")
        t.start()

    def stop(self) -> None:
        self.running = False
        sock = self.control_sock
        self.control_sock = None
        if sock:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                sock.close()
            except Exception:
                pass

    def _run_loop(self) -> None:
        logger.info("Starting PyWings Game Tunnel targeting %s:%d (Node ID: %s)...",
                    self.router_host, self.router_port, self.node_id)

        while self.running:
            try:
                self._connect_and_listen()
            except Exception as err:
                if self.running:
                    logger.warning("Game Tunnel disconnected/failed (%s); retrying in 5s...", err)
                    time.sleep(5)

    def _find_server_port(self, short_id: str) -> Optional[int]:
        short_id = short_id.lower()
        if not self.store:
            return None

        for s in self.store.all():
            if str(s.uuid).lower().startswith(short_id):
                cfg = s.configuration or {}
                allocs = cfg.get("allocations", {})
                if isinstance(allocs, dict) and "default" in allocs:
                    p = allocs["default"].get("port")
                    if p:
                        return int(p)

                # Fallback to environment SERVER_PORT
                env = cfg.get("environment", {})
                if "SERVER_PORT" in env:
                    try:
                        return int(env["SERVER_PORT"])
                    except Exception:
                        pass
        return None

    def _connect_and_listen(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        sock.connect((self.router_host, self.router_port))
        self.control_sock = sock

        # Rejestracja noda
        sock.sendall(f"REGISTER {self.node_id}\n".encode("utf-8"))
        logger.info("PyWings Game Tunnel REGISTERED with %s:%d", self.router_host, self.router_port)

        rfile = sock.makefile("r", encoding="utf-8")
        while self.running:
            line = rfile.readline()
            if not line:
                break

            parts = line.strip().split()
            if not parts:
                continue

            cmd = parts[0].upper()

            if cmd == "RESOLVE" and len(parts) >= 2:
                s_id = parts[1].lower()
                port = self._find_server_port(s_id)
                if port:
                    logger.info("Auto-resolved server %s to local port %d on node %s", s_id, port, self.node_id)
                    sock.sendall(f"RESOLVED {s_id} {port}\n".encode("utf-8"))

            elif cmd == "OPEN" and len(parts) == 3:
                stream_id = parts[1]
                target_port = int(parts[2])
                threading.Thread(
                    target=self._bridge_stream,
                    args=(stream_id, target_port),
                    daemon=True,
                ).start()

    def _bridge_stream(self, stream_id: str, target_port: int) -> None:
        try:
            # 1. Połącz z lokalnym serwerem gry na nodzie (127.0.0.1:port)
            local_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            local_sock.settimeout(3.0)
            local_sock.connect(("127.0.0.1", target_port))
            local_sock.settimeout(None)

            # 2. Otwórz dedykowany socket mostkujący do routera
            router_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            router_sock.settimeout(4.0)
            router_sock.connect((self.router_host, self.router_port))
            router_sock.settimeout(None)

            # 3. Zgłoś stream_id
            router_sock.sendall(f"BRIDGE {stream_id}\n".encode("utf-8"))

            # 4. Mostkuj strumień lokalnego serwera Minecraft z routerem
            bridge_sockets(local_sock, router_sock)

        except Exception as err:
            logger.debug("Failed bridging stream %s to 127.0.0.1:%d: %s", stream_id, target_port, err)
