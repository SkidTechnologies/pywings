"""Automatic updater for pywings daemon checking Git origin and restarting in-place."""

import logging
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Any


logger = logging.getLogger("wings.updater")


class AutoUpdater:
    """Monitors remote repository for updates, pulls changes, and restarts gracefully."""

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
        self.repo_dir = Path(__file__).resolve().parent.parent
        self._running = False
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        if not self.enabled:
            logger.info("Auto-updater is disabled by configuration (WINGS_AUTO_UPDATE=false).")
            return

        if not (self.repo_dir / ".git").is_dir():
            logger.info("Auto-updater disabled: .git directory not found in %s", self.repo_dir)
            return

        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="pywings-autoupdater")
        self._thread.start()
        logger.info("Auto-updater started (interval=%ds, branch=%s)", self.interval_seconds, self.branch)

    def stop(self) -> None:
        self._running = False

    def _run_loop(self) -> None:
        # Initial wait so daemon boot and server restores complete first
        time.sleep(10)
        while self._running:
            try:
                has_update, local_sha, remote_sha = self.check_update()
                if has_update:
                    logger.info("New pywings update detected! (local: %s -> remote: %s)", local_sha, remote_sha)
                    self.apply_update_and_restart(local_sha, remote_sha)
            except Exception as err:
                logger.debug("Auto-update check encountered error: %s", err)

            time.sleep(self.interval_seconds)

    def check_update(self) -> tuple[bool, str, str]:
        """Fetch remote refs and compare current HEAD with origin/<branch>."""
        with self._lock:
            # 1. Fetch remote changes without modifying working tree
            res = subprocess.run(
                ["git", "fetch", "origin", self.branch],
                cwd=str(self.repo_dir),
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if res.returncode != 0:
                logger.debug("git fetch failed (%d): %s", res.returncode, res.stderr.strip())
                return False, "", ""

            # 2. Get local HEAD commit
            local_proc = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=str(self.repo_dir),
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            local_sha = local_proc.stdout.strip()

            # 3. Get remote branch commit
            remote_proc = subprocess.run(
                ["git", "rev-parse", f"origin/{self.branch}"],
                cwd=str(self.repo_dir),
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            remote_sha = remote_proc.stdout.strip()

            if not local_sha or not remote_sha:
                return False, local_sha[:7], remote_sha[:7]

            return (local_sha != remote_sha), local_sha[:7], remote_sha[:7]

    def apply_update_and_restart(self, local_sha: str = "", remote_sha: str = "") -> bool:
        """Pull latest code, check requirements, and restart pywings process."""
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

            logger.info("Applying update (%s -> %s)...", local_sha, remote_sha)
            # Cleanly pull or reset to origin/<branch>
            pull_res = subprocess.run(
                ["git", "pull", "--ff-only", "origin", self.branch],
                cwd=str(self.repo_dir),
                capture_output=True,
                text=True,
                timeout=45,
                check=False,
            )
            if pull_res.returncode != 0:
                # Fallback to fetch + reset --hard if ff-only failed due to local timestamps
                logger.warning("git pull --ff-only failed, performing clean git reset --hard origin/%s", self.branch)
                subprocess.run(
                    ["git", "reset", "--hard", f"origin/{self.branch}"],
                    cwd=str(self.repo_dir),
                    capture_output=True,
                    timeout=30,
                    check=False,
                )

            logger.info("Update successfully applied! Restarting pywings process...")

            # Cleanly stop SFTP server if running before execv
            if self.app and "sftp_server" in self.app.extensions:
                try:
                    self.app.extensions["sftp_server"].stop()
                except Exception:
                    pass

            # Give a brief moment for HTTP/log buffers to flush
            time.sleep(1)

            # Re-execute current Python interpreter with same arguments
            python_bin = sys.executable
            args = [python_bin] + sys.argv
            logger.info("Executing: %s", " ".join(args))
            try:
                os.execv(python_bin, args)
            except Exception as err:
                logger.error("os.execv failed: %s; exiting for systemd/supervisor restart", err)
                sys.exit(0)

        return True
