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
        clean_target = short_id.lower().replace("-", "")
        if not self.store:
            return None

        for s in self.store.all():
            clean_uuid = str(s.uuid).lower().replace("-", "")
            if clean_uuid.startswith(clean_target):
                cfg = s.configuration or {}

                # 1. Check allocations["default"]["port"]
                allocs = cfg.get("allocations") or {}
                if isinstance(allocs, dict):
                    def_alloc = allocs.get("default")
                    if isinstance(def_alloc, dict) and def_alloc.get("port"):
                        try:
                            return int(def_alloc["port"])
                        except Exception:
                            pass
                    # mappings: {"0.0.0.0": [25565]}
                    mappings = allocs.get("mappings")
                    if isinstance(mappings, dict):
                        for port_list in mappings.values():
                            if isinstance(port_list, (list, tuple)) and port_list:
                                try:
                                    return int(port_list[0])
                                except Exception:
                                    pass

                # 2. Check environment["SERVER_PORT"]
                env = cfg.get("environment") or {}
                if isinstance(env, dict) and "SERVER_PORT" in env:
                    try:
                        return int(env["SERVER_PORT"])
                    except Exception:
                        pass

                # 3. Direct check: server.properties on disk
                try:
                    data_dir = getattr(self.store, "data_directory", None)
                    if data_dir:
                        props_path = Path(data_dir) / str(s.uuid) / "server.properties"
                        if props_path.is_file():
                            for pline in props_path.read_text(encoding="utf-8", errors="ignore").splitlines():
                                pline = pline.strip()
                                if pline.startswith("server-port="):
                                    p_val = pline.split("=", 1)[1].strip()
                                    if p_val.isdigit():
                                        return int(p_val)
                except Exception:
                    pass

        return None

    def _connect_and_listen(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        sock.connect((self.router_host, self.router_port))
        self.control_sock = sock

        # Rejestracja noda ze wszystkimi identyfikatorami (UUID + hostname)
        hname = socket.gethostname()
        sock.sendall(f"REGISTER {self.node_id} {hname}\n".encode("utf-8"))
        logger.info("PyWings Game Tunnel REGISTERED with %s:%d (UUID: %s, Hostname: %s)",
                    self.router_host, self.router_port, self.node_id, hname)

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
                    logger.info("Auto-resolved server %s to port %d on node %s (%s)", s_id, port, self.node_id, hname)
                    sock.sendall(f"RESOLVED {s_id} {port} {self.node_id} {hname}\n".encode("utf-8"))

            elif cmd == "OPEN" and len(parts) == 3:
                stream_id = parts[1]
                target_port = int(parts[2])
                threading.Thread(
                    target=self._bridge_stream,
                    args=(stream_id, target_port),
                    daemon=True,
                ).start()

    def _get_candidate_hosts(self, target_port: int) -> list[str]:
        hosts = ["127.0.0.1", "localhost"]
        try:
            hname = socket.gethostname()
            for info in socket.getaddrinfo(hname, None, socket.AF_INET):
                ip = info[4][0]
                if ip and ip not in hosts:
                    hosts.append(ip)
        except Exception:
            pass

        if self.store:
            for s in self.store.all():
                cfg = s.configuration or {}
                allocs = cfg.get("allocations") or {}
                if isinstance(allocs, dict):
                    def_alloc = allocs.get("default") or {}
                    if def_alloc.get("port") == target_port and def_alloc.get("ip"):
                        cand = str(def_alloc["ip"]).strip()
                        if cand and cand != "0.0.0.0" and cand not in hosts:
                            hosts.append(cand)
        return hosts

    def _bridge_stream(self, stream_id: str, target_port: int) -> None:
        local_sock = None
        router_sock = None
        try:
            # 1. Połącz z lokalnym serwerem gry na nodzie (próba 127.0.0.1, localhost, IP interfejsów)
            candidates = self._get_candidate_hosts(target_port)
            connected = False
            for host_cand in candidates:
                try:
                    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    s.settimeout(2.0)
                    s.connect((host_cand, target_port))
                    s.settimeout(None)
                    local_sock = s
                    connected = True
                    break
                except Exception:
                    try:
                        s.close()
                    except Exception:
                        pass

            if not connected:
                logger.warning(
                    "[GameTunnel] Cannot connect to local Minecraft server on port %d (checked: %s)! "
                    "Make sure the server is ONLINE and RUNNING in Pterodactyl!",
                    target_port, candidates,
                )
                # Otwórz i zamknij bridge z informacją o błędzie, żeby router nie czekał 5s
                try:
                    fail_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    fail_sock.settimeout(2.0)
                    fail_sock.connect((self.router_host, self.router_port))
                    fail_sock.sendall(f"BRIDGE {stream_id} FAIL\n".encode("utf-8"))
                    fail_sock.close()
                except Exception:
                    pass
                return

            # 2. Otwórz dedykowany socket mostkujący do routera
            router_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            router_sock.settimeout(4.0)
            router_sock.connect((self.router_host, self.router_port))
            router_sock.settimeout(None)

            # 3. Zgłoś stream_id
            router_sock.sendall(f"BRIDGE {stream_id}\n".encode("utf-8"))

            # 4. Mostkuj strumień lokalnego serwera Minecraft z routerem
            logger.info("[GameTunnel] Bridging stream %s to local port %d", stream_id, target_port)
            bridge_sockets(local_sock, router_sock)

        except Exception as err:
            logger.warning("[GameTunnel] Error bridging stream %s to port %d: %s", stream_id, target_port, err)
            if local_sock:
                try:
                    local_sock.close()
                except Exception:
                    pass
            if router_sock:
                try:
                    router_sock.close()
                except Exception:
                    pass
