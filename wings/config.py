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


def _get_version() -> str:
    v_file = PROJECT_ROOT / "version.txt"
    if v_file.is_file():
        try:
            return v_file.read_text(encoding="utf-8").strip() or "1.0.23-pywings"
        except Exception:
            pass
    return "1.0.23-pywings"


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
    sftp_bind_address: str = "0.0.0.0"
    sftp_cluster_host: str = "37.187.152.166"
    sftp_cluster_port: int = 2781
    allowed_mounts: tuple[str, ...] = ()
    remote: str = ""
    version: str = "1.0.23-pywings"
    config_path: Path = DEFAULT_CONFIG_PATH
    proot_path: str = ""

    @classmethod
    def from_file(cls, path: Path = DEFAULT_CONFIG_PATH) -> "Settings":
        resolved_path = path
        if not resolved_path.exists():
            candidates = [
                Path(os.getenv("WINGS_CONFIG_FILE", "")),
                Path(os.getenv("CONFIG_PATH", "")),
                PROJECT_ROOT / "config.yml",
                Path.cwd() / "config.yml",
                Path("/etc/pterodactyl/config.yml"),
            ]
            for c in candidates:
                if str(c) and c.is_file():
                    resolved_path = c
                    break

        values: dict[str, Any] = {}
        if resolved_path.exists():
            with resolved_path.open("r", encoding="utf-8") as config_file:
                loaded = yaml.safe_load(config_file) or {}
            if not isinstance(loaded, dict):
                raise ValueError(f"configuration root must be a mapping: {resolved_path}")
            values = loaded

        api = values.get("api") or {}
        ssl = api.get("ssl") or {}
        system = values.get("system") or {}

        # Highly flexible SFTP configuration parsing
        sftp = values.get("sftp")
        sftp_port_val = 2022
        sftp_addr_val = "0.0.0.0"
        allowed_mounts_list: list[str] = []

        if isinstance(sftp, dict):
            raw_p = sftp.get("bind_port") or sftp.get("port") or sftp.get("bind")
            if raw_p is not None:
                try:
                    sftp_port_val = int(str(raw_p).strip())
                except (ValueError, TypeError):
                    pass
            raw_a = sftp.get("bind_address") or sftp.get("address") or sftp.get("host")
            if raw_a:
                sftp_addr_val = str(raw_a).strip()
            allowed_mounts_list = [str(item) for item in (sftp.get("allowed_mounts") or [])]
        elif isinstance(sftp, (int, str)):
            try:
                sftp_port_val = int(str(sftp).strip())
            except (ValueError, TypeError):
                pass
        else:
            top_p = values.get("sftp_bind_port") or values.get("sftp_port")
            if top_p is not None:
                try:
                    sftp_port_val = int(str(top_p).strip())
                except (ValueError, TypeError):
                    pass
            top_a = values.get("sftp_bind_address") or values.get("sftp_address")
            if top_a:
                sftp_addr_val = str(top_a).strip()

        sftp_port = _env_int(
            "WINGS_SFTP_PORT",
            _env_int("SFTP_BIND_PORT", _env_int("SFTP_PORT", int(sftp_port_val)))
        )

        data_directory = Path(str(system.get("data", "./data")))
        if not data_directory.is_absolute():
            data_directory = resolved_path.parent / data_directory

        proot_path_val = (
            os.getenv("PROOT_PATH")
            or os.getenv("PROOT_BIN")
            or str(system.get("proot_path") or "")
        )

        raw_host = str(_env("WINGS_HOST", api.get("host", "0.0.0.0")))
        clean_host = raw_host.partition(":")[0] if ":" in raw_host and not raw_host.startswith("[") else raw_host

        raw_sftp_address = str(_env("WINGS_SFTP_ADDRESS", _env("SFTP_BIND_ADDRESS", _env("SFTP_ADDRESS", sftp_addr_val))))
        clean_sftp_address = raw_sftp_address.partition(":")[0] if ":" in raw_sftp_address and not raw_sftp_address.startswith("[") else raw_sftp_address

        cluster_host = str(_env("SFTP_CLUSTER_HOST", values.get("sftp_cluster_host", "37.187.152.166")))
        cluster_port = _env_int("SFTP_CLUSTER_PORT", int(values.get("sftp_cluster_port", 2781)))

        return cls(
            debug=_env_bool("WINGS_DEBUG", bool(values.get("debug", False))),
            uuid=str(_env("WINGS_UUID", values.get("uuid", ""))),
            token_id=str(_env("WINGS_TOKEN_ID", values.get("token_id", ""))),
            token=str(_env("WINGS_TOKEN", values.get("token", ""))),
            host=clean_host,
            port=_env_int("WINGS_PORT", int(api.get("port", 8080))),
            ssl_enabled=_env_bool("WINGS_SSL_ENABLED", bool(ssl.get("enabled", False))),
            ssl_cert=str(_env("WINGS_SSL_CERT", ssl.get("cert", ""))),
            ssl_key=str(_env("WINGS_SSL_KEY", ssl.get("key", ""))),
            upload_limit=int(api.get("upload_limit", 100)),
            data_directory=str(data_directory.resolve()),
            sftp_bind_port=sftp_port,
            sftp_bind_address=clean_sftp_address,
            sftp_cluster_host=cluster_host,
            sftp_cluster_port=cluster_port,
            allowed_mounts=tuple(allowed_mounts_list or [str(item) for item in (values.get("allowed_mounts") or [])]),
            remote=str(_env("WINGS_REMOTE", values.get("remote", ""))),
            version=str(_env("WINGS_VERSION", _get_version())),
            config_path=resolved_path,
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
            "SFTP_BIND_ADDRESS": self.sftp_bind_address,
            "SFTP_CLUSTER_HOST": self.sftp_cluster_host,
            "SFTP_CLUSTER_PORT": self.sftp_cluster_port,
            "ALLOWED_MOUNTS": self.allowed_mounts,
            "CONFIG_PATH": str(self.config_path),
            "PROOT_PATH": self.proot_path,
        }
