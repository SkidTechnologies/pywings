"""Configuration loading compatible with a Pterodactyl node config.yml."""

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any

import yaml


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yml"


def _env(name: str, default: Any) -> Any:
    value = os.getenv(name)
    return default if value is None else value


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    return int(value)


@dataclass(frozen=True)
class Settings:
    """Runtime settings extracted from the local Wings configuration."""

    debug: bool = False
    uuid: str = ""
    token_id: str = ""
    token: str = ""
    host: str = "0.0.0.0"
    port: int = 8080
    ssl_enabled: bool = False
    ssl_cert: str = ""
    ssl_key: str = ""
    upload_limit: int = 100
    data_directory: str = "./data"
    sftp_bind_port: int = 2022
    allowed_mounts: tuple[str, ...] = ()
    remote: str = ""
    version: str = "0.1.0"
    config_path: Path = DEFAULT_CONFIG_PATH
    proot_path: str = ""

    @classmethod
    def from_file(cls, path: Path = DEFAULT_CONFIG_PATH) -> "Settings":
        values: dict[str, Any] = {}
        if path.exists():
            with path.open("r", encoding="utf-8") as config_file:
                loaded = yaml.safe_load(config_file) or {}
            if not isinstance(loaded, dict):
                raise ValueError(f"configuration root must be a mapping: {path}")
            values = loaded

        api = values.get("api") or {}
        ssl = api.get("ssl") or {}
        system = values.get("system") or {}
        sftp = values.get("sftp") or {}

        data_directory = Path(str(system.get("data", "./data")))
        if not data_directory.is_absolute():
            data_directory = path.parent / data_directory

        proot_path_val = (
            os.getenv("PROOT_PATH")
            or os.getenv("PROOT_BIN")
            or str(system.get("proot_path") or "")
        )

        return cls(
            debug=_env_bool("WINGS_DEBUG", bool(values.get("debug", False))),
            uuid=str(_env("WINGS_UUID", values.get("uuid", ""))),
            token_id=str(_env("WINGS_TOKEN_ID", values.get("token_id", ""))),
            token=str(_env("WINGS_TOKEN", values.get("token", ""))),
            host=str(_env("WINGS_HOST", api.get("host", "0.0.0.0"))),
            port=_env_int("WINGS_PORT", int(api.get("port", 8080))),
            ssl_enabled=_env_bool("WINGS_SSL_ENABLED", bool(ssl.get("enabled", False))),
            ssl_cert=str(_env("WINGS_SSL_CERT", ssl.get("cert", ""))),
            ssl_key=str(_env("WINGS_SSL_KEY", ssl.get("key", ""))),
            upload_limit=int(api.get("upload_limit", 100)),
            data_directory=str(data_directory.resolve()),
            sftp_bind_port=int(sftp.get("bind_port", 2022)),
            allowed_mounts=tuple(str(item) for item in (sftp.get("allowed_mounts") or [])),
            remote=str(_env("WINGS_REMOTE", values.get("remote", ""))),
            version=str(_env("WINGS_VERSION", "0.1.0")),
            config_path=path,
            proot_path=proot_path_val,
        )

    def as_flask_config(self) -> dict[str, object]:
        return {
            "DEBUG": self.debug,
            "HOST": self.host,
            "PORT": self.port,
            "TOKEN": self.token,
            "TOKEN_ID": self.token_id,
            "UUID": self.uuid,
            "VERSION": self.version,
            "PANEL_LOCATION": self.remote,
            "SSL_ENABLED": self.ssl_enabled,
            "SSL_CERT": self.ssl_cert,
            "SSL_KEY": self.ssl_key,
            "UPLOAD_LIMIT": self.upload_limit,
            "DATA_DIRECTORY": self.data_directory,
            "SFTP_BIND_PORT": self.sftp_bind_port,
            "ALLOWED_MOUNTS": self.allowed_mounts,
            "CONFIG_PATH": str(self.config_path),
            "PROOT_PATH": self.proot_path,
        }
