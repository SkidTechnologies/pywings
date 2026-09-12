"""PRoot executable detection and validation."""

import logging
import os
from pathlib import Path
import shutil
import subprocess

from wings.runtime.base import RuntimeUnavailableError


logger = logging.getLogger("wings.runtime.proot")


class ProotDetector:
    """Discovers and validates the PRoot binary on the host."""

    @staticmethod
    def find_proot(configured_path: str | None = None) -> Path:
        """Find an executable PRoot binary or raise a detailed error."""
        candidates: list[str | Path | None] = [
            configured_path,
            os.environ.get("PROOT_PATH"),
            os.environ.get("PROOT_BIN"),
            "/home/container/.tools/proot",
            shutil.which("proot"),
            "/usr/bin/proot",
            "/usr/local/bin/proot",
            Path.home() / ".tools" / "proot",
            Path.home() / ".local" / "bin" / "proot",
        ]

        for cand in candidates:
            if not cand:
                continue
            path = Path(cand).expanduser().resolve()
            if path.is_file() and os.access(path, os.X_OK):
                # Verify execution
                try:
                    res = subprocess.run(
                        [str(path), "--version"],
                        capture_output=True,
                        text=True,
                        timeout=5,
                        check=False,
                    )
                    # PRoot typically returns 0 or prints version info
                    logger.info("Found PRoot binary at %s: %s", path, res.stdout.strip() or res.stderr.strip())
                    return path
                except Exception as err:
                    logger.debug("PRoot candidate %s failed execution check: %s", path, err)

        raise RuntimeUnavailableError(
            "PRoot executable was not found on this system.\n"
            "PRoot is required to provide user-space root emulation and filesystem isolation without Docker.\n"
            "To fix this:\n"
            "  1. Install proot via your package manager: apt-get install proot\n"
            "  2. Or download a static binary to /usr/local/bin/proot\n"
            "  3. Or configure PROOT_PATH in config.yml (e.g. system.proot_path: /path/to/proot)\n"
            "  4. Or set the PROOT_PATH environment variable."
        )
