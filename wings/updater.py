"""Automatic updater for pywings checking version.txt on GitHub and updating in-place."""

import io
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
from typing import Any
import urllib.request
import zipfile


logger = logging.getLogger("wings.updater")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE = PROJECT_ROOT / "version.txt"
DEFAULT_VERSION = "1.0.23-pywings"
REMOTE_VERSION_URL = "https://raw.githubusercontent.com/SkidTechnologies/pywings/main/version.txt"
REMOTE_ARCHIVE_URL = "https://github.com/SkidTechnologies/pywings/archive/refs/heads/main.zip"


def parse_version(v_str: str) -> tuple[int, ...]:
    """Convert version string like '1.0.6-pywings' or 'v1.0.5' into tuple of integers."""
    clean = v_str.strip().lstrip("vV")
    base = clean.split("-")[0].split("+")[0]
    parts = []
    for piece in base.split("."):
        try:
            parts.append(int(piece))
        except ValueError:
            parts.append(0)
    return tuple(parts) or (0, 0, 0)


def get_local_version() -> str:
    """Read local version from version.txt."""
    if VERSION_FILE.is_file():
        try:
            return VERSION_FILE.read_text(encoding="utf-8").strip() or DEFAULT_VERSION
        except Exception:
            pass
    return DEFAULT_VERSION


def get_remote_version(timeout: int = 5) -> str | None:
    """Fetch remote version from GitHub instantly using API with raw fallback."""
    import base64

    # 1. GitHub API (instantaneous, never delayed by CDN cache)
    api_url = "https://api.github.com/repos/SkidTechnologies/pywings/contents/version.txt"
    try:
        req = urllib.request.Request(
            api_url,
            headers={"User-Agent": "pywings-autoupdater/1.0", "Accept": "application/vnd.github.v3+json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8"))
                if "content" in data:
                    raw = base64.b64decode(data["content"]).decode("utf-8", errors="replace").strip()
                    if raw:
                        return raw
    except Exception as err:
        logger.debug("GitHub API version check failed (%s), trying raw URL", err)

    # 2. Raw CDN fallback
    url = f"{REMOTE_VERSION_URL}?_t={int(time.time())}"
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "pywings-autoupdater/1.0",
                "Cache-Control": "no-cache, no-store, must-revalidate",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                content = resp.read().decode("utf-8", errors="replace").strip()
                if content:
                    return content
    except Exception:
        pass

    return None


def download_and_extract_archive(dest_dir: Path) -> bool:
    """Download ZIP archive from GitHub and extract files directly into destination directory."""
    print(f"[INFO] [updater] Downloading latest code from {REMOTE_ARCHIVE_URL}...", flush=True)
    req = urllib.request.Request(
        REMOTE_ARCHIVE_URL,
        headers={"User-Agent": "pywings-autoupdater/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            if resp.status != 200:
                print(f"[ERROR] [updater] GitHub archive download returned status {resp.status}", flush=True)
                return False
            data = resp.read()

        print(f"[INFO] [updater] Archive downloaded ({len(data)} bytes). Extracting files...", flush=True)
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            members = z.infolist()
            if not members:
                return False
            prefix = members[0].filename.split("/")[0] + "/"

            for member in members:
                if not member.filename.startswith(prefix):
                    continue
                rel_path = member.filename[len(prefix):]
                if not rel_path:
                    continue

                # Protect user configuration and server data
                if rel_path == "config.yml" or rel_path.startswith("data/"):
                    continue

                target_file = dest_dir / rel_path
                if member.is_dir():
                    target_file.mkdir(parents=True, exist_ok=True)
                else:
                    target_file.parent.mkdir(parents=True, exist_ok=True)
                    with z.open(member) as src, open(target_file, "wb") as dst:
                        shutil.copyfileobj(src, dst)

        print("[INFO] [updater] Extraction completed successfully.", flush=True)
        return True
    except Exception as err:
        print(f"[ERROR] [updater] Failed downloading/extracting GitHub archive: {err}", flush=True)
        return False


def apply_update_from_github(repo_dir: Path, branch: str = "main") -> bool:
    """Update files either via Git or direct GitHub zip extraction."""
    has_git = (repo_dir / ".git").is_dir()

    if has_git:
        print(f"[INFO] [updater] Updating via Git from origin/{branch}...", flush=True)
        res = subprocess.run(
            ["git", "fetch", "origin", branch],
            cwd=str(repo_dir),
            capture_output=True,
            timeout=30,
            check=False,
        )
        if res.returncode == 0:
            reset_res = subprocess.run(
                ["git", "reset", "--hard", f"origin/{branch}"],
                cwd=str(repo_dir),
                capture_output=True,
                timeout=30,
                check=False,
            )
            if reset_res.returncode == 0:
                print(f"[INFO] [updater] Git reset to origin/{branch} succeeded.", flush=True)
                return True

    # Direct zip download fallback (works for standalone unzipped pywings-main without .git)
    return download_and_extract_archive(repo_dir)


def check_and_update_on_launch() -> None:
    """Called at application launch to immediately upgrade to newer GitHub version if available."""
    local_ver = get_local_version()
    print(f"[INFO] [updater] Checking GitHub for updates (local version: v{local_ver})...", flush=True)

    remote_ver = get_remote_version(timeout=6)
    if not remote_ver:
        print(f"[INFO] [updater] GitHub check unavailable; running local version v{local_ver}", flush=True)
        return

    print(f"[INFO] [updater] GitHub version: v{remote_ver} | Local version: v{local_ver}", flush=True)
    if parse_version(remote_ver) > parse_version(local_ver):
        print("=" * 60, flush=True)
        print(f"[INFO] [updater] NEW UPDATE AVAILABLE: v{remote_ver} (current: v{local_ver})", flush=True)
        print("[INFO] [updater] Downloading and applying update from GitHub...", flush=True)
        print("=" * 60, flush=True)

        success = apply_update_from_github(PROJECT_ROOT)
        if success:
            print(f"[INFO] [updater] Successfully updated to v{remote_ver}! Relaunching pywings...", flush=True)
            time.sleep(1)
            python_bin = sys.executable
            args = [python_bin] + sys.argv
            try:
                os.execv(python_bin, args)
            except Exception as err:
                print(f"[ERROR] [updater] os.execv failed: {err}; exiting for process restart", flush=True)
                sys.exit(0)
        else:
            print(f"[WARN] [updater] Update could not be applied; continuing with current version v{local_ver}", flush=True)
    else:
        print(f"[INFO] [updater] pywings is up-to-date (v{local_ver})", flush=True)


class AutoUpdater:
    """Background service checking GitHub version.txt periodically and restarting in-place."""

    def __init__(
        self,
        app: Any = None,
        interval_seconds: int = 60,
        enabled: bool = True,
        branch: str = "main",
    ) -> None:
        self.app = app
        self.interval_seconds = max(15, interval_seconds)
        self.enabled = enabled
        self.branch = branch
        self.repo_dir = PROJECT_ROOT
        self._running = False
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        if not self.enabled:
            logger.info("Auto-updater is disabled by configuration (WINGS_AUTO_UPDATE=false).")
            return

        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="pywings-autoupdater")
        self._thread.start()
        logger.info("Auto-updater background worker started (interval=%ds, branch=%s)", self.interval_seconds, self.branch)

    def stop(self) -> None:
        self._running = False

    def _run_loop(self) -> None:
        time.sleep(15)
        while self._running:
            try:
                local_ver = get_local_version()
                remote_ver = get_remote_version(timeout=10)
                if remote_ver and parse_version(remote_ver) > parse_version(local_ver):
                    logger.info("New pywings update detected via version.txt (v%s -> v%s)! Updating...", local_ver, remote_ver)
                    self.apply_update_and_restart(local_ver, remote_ver)
            except Exception as err:
                logger.debug("Auto-update check encountered error: %s", err)

            time.sleep(self.interval_seconds)

    def check_update(self) -> tuple[bool, str, str]:
        """Check if newer version is available on GitHub."""
        local_ver = get_local_version()
        remote_ver = get_remote_version(timeout=10)
        if not remote_ver:
            return False, local_ver, local_ver
        has_update = parse_version(remote_ver) > parse_version(local_ver)
        return has_update, local_ver, remote_ver

    def apply_update_and_restart(self, local_ver: str = "", remote_ver: str = "") -> bool:
        """Pull latest code and restart pywings process."""
        with self._lock:
            # Wait if any server is actively installing
            if self.app and "process_manager" in self.app.extensions:
                pm = self.app.extensions["process_manager"]
                for _ in range(30):
                    installing = False
                    for srv in pm.store.all():
                        if srv.state == "installing":
                            installing = True
                            break
                    if not installing:
                        break
                    logger.info("Server installation active; delaying pywings update for 5 seconds...")
                    time.sleep(5)

            logger.info("Applying update (%s -> %s)...", local_ver, remote_ver)
            success = apply_update_from_github(self.repo_dir, self.branch)
            if not success:
                logger.warning("Could not apply update from GitHub.")
                return False

            logger.info("Update successfully applied! Restarting pywings process...")

            # Cleanly stop SFTP server if running before execv
            if self.app and "sftp_server" in self.app.extensions:
                try:
                    self.app.extensions["sftp_server"].stop()
                except Exception:
                    pass

            # Close all inherited socket file descriptors (except stdin 0, stdout 1, stderr 2)
            try:
                import resource
                max_fd = resource.getrlimit(resource.RLIMIT_NOFILE)[1]
                if max_fd == resource.RLIM_INFINITY:
                    max_fd = 1024
                os.closerange(3, min(max_fd, 4096))
            except Exception:
                pass

            time.sleep(1)

            python_bin = sys.executable
            args = [python_bin] + sys.argv
            logger.info("Executing: %s", " ".join(args))
            try:
                os.execv(python_bin, args)
            except Exception as err:
                logger.error("os.execv failed: %s; exiting for systemd/supervisor restart", err)
                sys.exit(0)

        return True
