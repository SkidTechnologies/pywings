"""Interactive and CLI configuration generator for pywings node config.yml."""

import argparse
from pathlib import Path
import sys
import yaml

from wings.config import PROJECT_ROOT, DEFAULT_CONFIG_PATH


def run_configure(args: list[str]) -> None:
    """Handle wings configure command matching Pterodactyl node deployment."""
    parser = argparse.ArgumentParser(description="Configure pywings node from Pterodactyl Panel")
    parser.add_argument("--panel-url", dest="panel_url", help="URL of the Pterodactyl Panel", default=None)
    parser.add_argument("--token", dest="token", help="Node deployment or client token", default=None)
    parser.add_argument("--token-id", dest="token_id", help="Token ID if applicable", default="")
    parser.add_argument("--node", dest="node_id", help="Node UUID or ID", default="")
    parser.add_argument("--config", dest="config_path", help="Path to config.yml", default=str(DEFAULT_CONFIG_PATH))

    parsed, remaining = parser.parse_known_args(args)
    target_path = Path(parsed.config_path).resolve()

    print("=" * 65)
    print("           pywings Node Configuration Helper")
    print("=" * 65)

    existing: dict = {}
    if target_path.is_file():
        try:
            with target_path.open("r", encoding="utf-8") as f:
                existing = yaml.safe_load(f) or {}
        except Exception:
            existing = {}

    panel_url = parsed.panel_url or existing.get("remote")
    token = parsed.token or existing.get("token")
    token_id = parsed.token_id or existing.get("token_id", "")

    if not panel_url or not token:
        print("\nPaste your Pterodactyl Node configuration YAML below.")
        print("(Press Ctrl+D on Linux or Ctrl+Z then Enter on Windows when finished):\n")
        try:
            input_text = sys.stdin.read().strip()
            if input_text:
                try:
                    loaded = yaml.safe_load(input_text)
                    if isinstance(loaded, dict):
                        target_path.write_text(yaml.safe_dump(loaded, sort_keys=False), encoding="utf-8")
                        print(f"\n[OK] Configuration successfully parsed and written to {target_path}!")
                        return
                except Exception as err:
                    print(f"[WARN] Could not parse YAML input: {err}")
        except (KeyboardInterrupt, EOFError):
            pass

    # If still missing, prompt interactively
    if not panel_url:
        panel_url = input("Panel URL (e.g. https://panel.example.com): ").strip()
    if not token_id:
        token_id = input("Token ID: ").strip()
    if not token:
        token = input("Token: ").strip()

    config_data = {
        "debug": False,
        "uuid": parsed.node_id or existing.get("uuid", ""),
        "token_id": token_id,
        "token": token,
        "remote": panel_url.rstrip("/"),
        "api": {
            "host": "0.0.0.0",
            "port": 8080,
            "ssl": {
                "enabled": False,
                "cert": "",
                "key": "",
            },
            "upload_limit": 100,
        },
        "system": {
            "data": "./data",
            "sftp": {
                "bind_address": "0.0.0.0",
                "bind_port": 2022,
            },
        },
        "allowed_mounts": [],
    }

    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(yaml.safe_dump(config_data, sort_keys=False), encoding="utf-8")
    print(f"\n[OK] Configuration successfully saved to: {target_path}")
    print("You can now start pywings by running: python3 app.py")
