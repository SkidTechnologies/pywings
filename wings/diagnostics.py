"""Diagnostic reporting tool for pywings node and environment."""

import os
from pathlib import Path
import platform
import shutil
import sys
import urllib.request

from wings.config import Settings
from wings.runtime.proot_detector import ProotDetector


def run_diagnostics(args: list[str] | None = None) -> None:
    """Print comprehensive node diagnostics to stdout."""
    print("=" * 65)
    print("           pywings Node Diagnostics Report")
    print("=" * 65)

    # 1. System Information
    print("\n[System Information]")
    print(f"  OS:           {platform.platform()}")
    print(f"  Architecture: {platform.machine()}")
    print(f"  Kernel:       {platform.release()}")
    print(f"  Python:       {platform.python_version()} ({sys.executable})")
    print(f"  CPU Count:    {os.cpu_count() or 1}")

    # Memory / Disk
    try:
        data_dir = Path("./data").resolve()
        stat = shutil.disk_usage(data_dir if data_dir.exists() else Path.cwd())
        total_gb = stat.total / (1024 ** 3)
        free_gb = stat.free / (1024 ** 3)
        print(f"  Disk Space:   {free_gb:.2f} GB free / {total_gb:.2f} GB total ({data_dir})")
    except Exception:
        pass

    # 2. PyWings & Runtime
    print("\n[pywings & Runtime]")
    settings = Settings.from_file()
    print(f"  Version:      {settings.version}")
    print(f"  API Bind:     {settings.host}:{settings.port}")
    print(f"  SFTP Bind:    {settings.sftp_bind_address}:{settings.sftp_bind_port}")
    print(f"  Data Dir:     {settings.data_directory}")

    try:
        proot_path = ProotDetector.find_proot(settings.proot_path)
        print(f"  PRoot Path:   {proot_path} (OK)")
        print(f"  Supports -n:  {ProotDetector.supports_no_seccomp()}")
    except Exception as err:
        print(f"  PRoot Status: ERROR ({err})")

    # 3. Panel Connectivity
    print("\n[Pterodactyl Panel Connectivity]")
    panel_url = settings.remote
    if not panel_url:
        print("  Status:       NOT CONFIGURED (system.remote / api.remote is empty)")
    else:
        print(f"  Panel URL:    {panel_url}")
        print(f"  Token ID:     {settings.token_id or '(none)'}")
        test_url = panel_url.rstrip("/") + "/api/remote"
        try:
            req = urllib.request.Request(
                test_url,
                headers={"User-Agent": "pywings-diagnostics/1.0"},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                print(f"  Connection:   SUCCESS (HTTP {resp.status})")
        except urllib.error.HTTPError as err:
            # 401/403 means server responded, so network connection succeeded!
            if err.code in (401, 403):
                print(f"  Connection:   SUCCESS (Panel reached, HTTP {err.code})")
            else:
                print(f"  Connection:   WARNING (Panel returned HTTP {err.code})")
        except Exception as err:
            print(f"  Connection:   FAILED ({err})")

    # 4. Servers
    servers_file = Path(settings.data_directory) / "servers.json"
    if servers_file.is_file():
        import json
        try:
            srv_list = json.loads(servers_file.read_text(encoding="utf-8"))
            print("\n[Server Storage]")
            print(f"  Stored Servers: {len(srv_list)}")
            for s in srv_list[:5]:
                print(f"    - {s.get('uuid')} (state: {s.get('state', 'unknown')})")
        except Exception:
            pass

    print("\n" + "=" * 65)
    print("Diagnostics complete.")
