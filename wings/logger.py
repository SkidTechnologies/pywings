"""Extended console logging formatter and configuration for pywings."""

from datetime import datetime, timezone
import logging
import os
import sys
import time
from typing import Any


# ANSI color codes
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"

COLOR_MAP = {
    logging.DEBUG: "\033[36m",     # Cyan
    logging.INFO: "\033[32m",      # Green
    logging.WARNING: "\033[33m",   # Yellow
    logging.ERROR: "\033[31m",     # Red
    logging.CRITICAL: "\033[35m",  # Magenta
}

LEVEL_SHORT = {
    logging.DEBUG: "DEBU",
    logging.INFO: "INFO",
    logging.WARNING: "WARN",
    logging.ERROR: "ERRO",
    logging.CRITICAL: "FATL",
}


class WingsConsoleFormatter(logging.Formatter):
    """Clean, high-visibility log formatter modeled after Pterodactyl Wings."""

    def __init__(self, use_colors: bool = True) -> None:
        super().__init__()
        # Auto-detect color support if terminal attached
        self.use_colors = use_colors and (hasattr(sys.stdout, "isatty") and sys.stdout.isatty())
        if os.getenv("NO_COLOR") or os.getenv("TERM") == "dumb":
            self.use_colors = False

    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.fromtimestamp(record.created, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        level_label = LEVEL_SHORT.get(record.levelno, record.levelname[:4])
        logger_name = record.name
        if logger_name.startswith("wings."):
            logger_name = logger_name[6:]

        message = record.getMessage()

        if self.use_colors:
            color = COLOR_MAP.get(record.levelno, RESET)
            formatted = (
                f"{color}{BOLD}[{level_label}]{RESET} "
                f"{DIM}[{timestamp}]{RESET} "
                f"{BOLD}[{logger_name}]{RESET} "
                f"{message}"
            )
        else:
            formatted = f"[{level_label}] [{timestamp}] [{logger_name}] {message}"

        if record.exc_info:
            formatted += "\n" + self.formatException(record.exc_info)
        return formatted


def setup_logging(debug: bool = False) -> None:
    """Initialize extended console logging for the pywings application."""
    log_level = logging.DEBUG if debug else logging.INFO

    # Root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)

    # Remove existing handlers to avoid duplicates
    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(log_level)
    handler.setFormatter(WingsConsoleFormatter(use_colors=True))
    root_logger.addHandler(handler)

    # Set logger levels for sub-modules
    for mod in ("wings", "wings.api", "wings.processes", "wings.remote", "wings.sftp", "wings.udocker", "wings.events"):
        l = logging.getLogger(mod)
        l.setLevel(log_level)
        l.propagate = True

    # Adjust external libraries (werkzeug / urllib / paramiko)
    logging.getLogger("werkzeug").setLevel(logging.INFO if debug else logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("paramiko").setLevel(logging.INFO if debug else logging.WARNING)

    root_logger.info("Extended console logging initialized (debug=%s)", debug)
