"""Egg configuration file updater matching Wings parser functionality."""

import json
import logging
from pathlib import Path
import re
from typing import Any
import yaml


logger = logging.getLogger("wings.parser")

CONFIG_MATCH_REGEX = re.compile(r"{{\s?config\.([\w.-]+)\s?}}")
ENV_MATCH_REGEX = re.compile(r"{{\s?env\.([\w.-]+)\s?}}")
SERVER_VAR_REGEX = re.compile(r"{{\s?([A-Za-z0-9_.-]+)\s?}}")


class ConfigParser:
    """Parses and modifies server configuration files before startup."""

    def __init__(self, server_root: Path, configuration: dict[str, Any], environment: dict[str, str]) -> None:
        self.server_root = Path(server_root).resolve()
        self.configuration = configuration
        self.environment = environment

    def resolve_value(self, template_str: str) -> str:
        """Replace {{config.xxx}}, {{env.xxx}}, and {{VARIABLE}} placeholders."""
        val = str(template_str)

        # {{config.docker.interface}} or similar
        def replace_config(match: re.Match) -> str:
            key = match.group(1).lower()
            if "interface" in key:
                return "0.0.0.0"
            return match.group(0)

        val = CONFIG_MATCH_REGEX.sub(replace_config, val)

        # {{env.XXX}}
        def replace_env(match: re.Match) -> str:
            var_name = match.group(1)
            return self.environment.get(var_name, "")

        val = ENV_MATCH_REGEX.sub(replace_env, val)

        # {{SERVER_PORT}}, {{SERVER_IP}}, etc.
        def replace_var(match: re.Match) -> str:
            var_name = match.group(1)
            if var_name in self.environment:
                return self.environment[var_name]
            # Check configuration allocations
            if var_name == "SERVER_PORT":
                ports = self.configuration.get("allocations", {}).get("default", {})
                if isinstance(ports, dict) and "port" in ports:
                    return str(ports["port"])
            return self.environment.get(var_name, match.group(0))

        val = SERVER_VAR_REGEX.sub(replace_var, val)
        return val

    def update_configuration_files(self) -> None:
        """Process all configuration files defined for this server."""
        process_config = self.configuration.get("process_configuration") or {}
        configs = process_config.get("configs") or []
        if not isinstance(configs, list):
            return

        for entry in configs:
            if not isinstance(entry, dict):
                continue
            filename = entry.get("file")
            parser_type = entry.get("parser", "file")
            replacements = entry.get("replace") or []
            if not filename or not isinstance(replacements, list):
                continue

            file_path = (self.server_root / filename).resolve()
            if not file_path.is_relative_to(self.server_root):
                logger.warning("Configuration file %s resolves outside server root, skipping", filename)
                continue

            try:
                self._update_file(file_path, parser_type, replacements)
            except Exception as err:
                logger.warning("Failed to update configuration file %s: %s", filename, err)

    def _update_file(self, file_path: Path, parser_type: str, replacements: list[dict]) -> None:
        if not file_path.exists():
            if parser_type in {"file", "text"}:
                return
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.touch()

        content = file_path.read_text(encoding="utf-8", errors="replace") if file_path.exists() else ""

        if parser_type == "properties":
            updated = self._update_properties(content, replacements)
        elif parser_type == "json":
            updated = self._update_json(content, replacements)
        elif parser_type in {"yaml", "yml"}:
            updated = self._update_yaml(content, replacements)
        elif parser_type == "ini":
            updated = self._update_ini(content, replacements)
        else:
            updated = self._update_text(content, replacements)

        if updated is not None and updated != content:
            file_path.write_text(updated, encoding="utf-8")
            logger.debug("Successfully updated configuration file: %s", file_path.name)

    def _update_properties(self, content: str, replacements: list[dict]) -> str:
        lines = content.splitlines()
        props: dict[str, int] = {}
        for idx, line in enumerate(lines):
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                key = stripped.split("=", 1)[0].strip()
                props[key] = idx

        for rep in replacements:
            match_key = rep.get("match", "")
            replace_with = self.resolve_value(rep.get("replace_with", rep.get("value", "")))
            if match_key in props:
                lines[props[match_key]] = f"{match_key}={replace_with}"
            else:
                lines.append(f"{match_key}={replace_with}")
        return "\n".join(lines) + ("\n" if lines else "")

    def _update_json(self, content: str, replacements: list[dict]) -> str:
        try:
            data = json.loads(content) if content.strip() else {}
        except Exception:
            data = {}

        for rep in replacements:
            match_path = rep.get("match", "")
            replace_with = self.resolve_value(rep.get("replace_with", rep.get("value", "")))
            # Try to convert to int/bool if appropriate
            parsed_val: Any = replace_with
            if replace_with.lower() == "true":
                parsed_val = True
            elif replace_with.lower() == "false":
                parsed_val = False
            elif replace_with.isdigit():
                parsed_val = int(replace_with)

            keys = match_path.split(".")
            current = data
            for k in keys[:-1]:
                if k not in current or not isinstance(current[k], dict):
                    current[k] = {}
                current = current[k]
            if keys:
                current[keys[-1]] = parsed_val

        return json.dumps(data, indent=2) + "\n"

    def _update_yaml(self, content: str, replacements: list[dict]) -> str:
        try:
            data = yaml.safe_load(content) if content.strip() else {}
            if not isinstance(data, dict):
                data = {}
        except Exception:
            data = {}

        for rep in replacements:
            match_path = rep.get("match", "")
            replace_with = self.resolve_value(rep.get("replace_with", rep.get("value", "")))
            keys = match_path.split(".")
            current = data
            for k in keys[:-1]:
                if k not in current or not isinstance(current[k], dict):
                    current[k] = {}
                current = current[k]
            if keys:
                current[keys[-1]] = replace_with

        return yaml.safe_dump(data, sort_keys=False)

    def _update_ini(self, content: str, replacements: list[dict]) -> str:
        return self._update_properties(content, replacements)

    def _update_text(self, content: str, replacements: list[dict]) -> str:
        result = content
        for rep in replacements:
            match_str = rep.get("match", "")
            replace_with = self.resolve_value(rep.get("replace_with", rep.get("value", "")))
            if match_str:
                result = result.replace(match_str, replace_with)
        return result
