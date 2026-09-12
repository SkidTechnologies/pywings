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

    _cached_path: Path | None = None

    @classmethod
    def find_proot(cls, configured_path: str | None = None) -> Path:
        """Find an executable PRoot binary or raise a detailed error."""
        if cls._cached_path and cls._cached_path.is_file() and os.access(cls._cached_path, os.X_OK):
            return cls._cached_path

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
                    out = (res.stdout or res.stderr or "").strip()
                    # Find version number or first non-empty line
                    ver_line = "found"
                    for line in out.splitlines():
                        line_s = line.strip()
                        if line_s and not line_s.startswith("|") and not line_s.startswith("_"):
                            ver_line = line_s
                            break
                    logger.info("Found PRoot binary at %s (%s)", path, ver_line)
                    cls._cached_path = path
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
