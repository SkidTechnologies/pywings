"""Panel remote client used to interact with Pterodactyl Panel's remote endpoints."""

import json
import logging
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from typing import Any


logger = logging.getLogger("wings.remote")


class PanelRemoteError(Exception):
    """Raised when a Panel remote API endpoint call fails."""


class PanelRemoteClient:
    def __init__(self, base_url: str, token_id: str, token: str) -> None:
        self.base_url = base_url.rstrip("/")
        if self.base_url and not self.base_url.endswith("/api/remote"):
            self.endpoint = f"{self.base_url}/api/remote"
        else:
            self.endpoint = self.base_url
        self.token_id = token_id
        self.token = token
        self._update_auth()

    def set_credentials(self, token_id: str, token: str) -> None:
        self.token_id = token_id
        self.token = token
        self._update_auth()

    def _update_auth(self) -> None:
        if self.token_id and self.token:
            self.authorization = f"Bearer {self.token_id}.{self.token}"
        else:
            self.authorization = ""

    def _request(
        self,
        method: str,
        path: str,
        data: dict | list | None = None,
        query: dict | None = None,
        timeout: int = 30,
        retries: int = 3,
    ) -> Any:
        if not self.base_url:
            raise PanelRemoteError("Panel remote URL is not configured")

        url = f"{self.endpoint}/{path.lstrip('/')}"
        if query:
            url = f"{url}?{urlencode(query)}"

        headers = {
            "Accept": "application/vnd.pterodactyl.v1+json",
            "Content-Type": "application/json",
            "User-Agent": "Pterodactyl Wings (pywings)",
        }
        if self.authorization:
            headers["Authorization"] = self.authorization

        body = None
        if data is not None:
            body = json.dumps(data).encode("utf-8")

        for attempt in range(1, retries + 1):
            req = Request(url, data=body, headers=headers, method=method)
            try:
                with urlopen(req, timeout=timeout) as response:
                    raw = response.read().decode("utf-8")
                    if not raw.strip():
                        return None
                    try:
                        return json.loads(raw)
                    except json.JSONDecodeError:
                        return raw
            except HTTPError as error:
                error_body = ""
                try:
                    error_body = error.read().decode("utf-8")
                except Exception:
                    pass
                if error.code in {400, 401, 403, 404, 422}:
                    logger.warning(
                        "Panel API %s %s returned HTTP %s: %s",
                        method,
                        path,
                        error.code,
                        error_body,
                    )
                    raise PanelRemoteError(
                        f"Panel API {method} {path} returned HTTP {error.code}: {error_body or error.reason}"
                    ) from error
                if attempt == retries:
                    raise PanelRemoteError(
                        f"Panel API {method} {path} returned HTTP {error.code} after {retries} attempts: {error_body or error.reason}"
                    ) from error
                time.sleep(1.0 * attempt)
            except (URLError, TimeoutError, OSError) as error:
                if attempt == retries:
                    logger.warning(
                        "Panel API %s %s request failed after %d attempts: %s",
                        method,
                        path,
                        retries,
                        error,
                    )
                    raise PanelRemoteError(f"Panel API {method} {path} connection failed: {error}") from error
                logger.debug(
                    "Panel API %s %s attempt %d failed (%s), retrying...",
                    method,
                    path,
                    attempt,
                    error,
                )
                time.sleep(1.0 * attempt)

    def get_server_configuration(self, server_uuid: str) -> dict:
        """Fetch server settings and process configuration from the Panel."""
        payload = self._request("GET", f"/servers/{server_uuid}")
        if not isinstance(payload, dict):
            raise PanelRemoteError("Panel returned an invalid server settings response")

        settings = payload.get("settings", payload)
        if isinstance(settings, str):
            try:
                settings = json.loads(settings)
            except json.JSONDecodeError:
                pass
        if not isinstance(settings, dict):
            raise PanelRemoteError("Panel returned an invalid server settings object")

        settings["uuid"] = server_uuid
        if "process_configuration" in payload and "process_configuration" not in settings:
            settings["process_configuration"] = payload["process_configuration"]

        return settings

    def get_installation_script(self, server_uuid: str) -> dict:
        """Fetch egg installation script for the given server from the Panel."""
        payload = self._request("GET", f"/servers/{server_uuid}/install")
        if not isinstance(payload, dict):
            raise PanelRemoteError("Panel returned an invalid installation script response")
        return payload

    def set_installation_status(
        self, server_uuid: str, successful: bool, reinstall: bool = False
    ) -> None:
        """Notify the Panel of server installation status (success or failure)."""
        logger.info(
            "Notifying Panel of server %s installation status (successful=%s, reinstall=%s)",
            server_uuid,
            successful,
            reinstall,
        )
        self._request(
            "POST",
            f"/servers/{server_uuid}/install",
            data={"successful": bool(successful), "reinstall": bool(reinstall)},
        )

    def reset_servers_state(self) -> None:
        """Reset state of installing or restoring servers on Panel boot."""
        try:
            self._request("POST", "/servers/reset")
            logger.info("Successfully reset installing/restoring server states on Panel")
        except PanelRemoteError as error:
            logger.warning("Failed to reset server states on Panel: %s", error)

    def set_archive_status(self, server_uuid: str, successful: bool) -> None:
        """Notify the Panel of server archive status."""
        self._request("POST", f"/servers/{server_uuid}/archive", data={"successful": bool(successful)})

    def set_backup_status(self, backup_uuid: str, data: dict) -> None:
        """Notify the Panel of completed/failed backup status."""
        self._request("POST", f"/backups/{backup_uuid}", data=data)

    def send_restoration_status(self, backup_uuid: str, successful: bool) -> None:
        """Notify the Panel of backup restoration status."""
        self._request("POST", f"/backups/{backup_uuid}/restore", data={"successful": bool(successful)})

    def set_transfer_status(self, server_uuid: str, successful: bool) -> None:
        """Notify the Panel of transfer status."""
        self._request("POST", f"/servers/{server_uuid}/transfer", data={"successful": bool(successful)})

    def validate_sftp_credentials(
        self,
        username: str,
        password: str,
        client_ip: str,
        session_id: str = "",
        client_version: str = "",
    ) -> dict:
        """Validate SFTP user credentials against the Panel."""
        payload = {
            "type": "password",
            "username": username,
            "password": password,
            "ip": client_ip,
            "session_id": session_id,
            "client_version": client_version,
        }
        resp = self._request("POST", "/sftp", data=payload)
        if not isinstance(resp, dict) or "server" not in resp:
            raise PanelRemoteError("Invalid SFTP authentication response from Panel")
        return resp

    def send_activity_logs(self, activity: list[dict]) -> None:
        """Send batched activity logs to the Panel."""
        if not activity:
            return
        try:
            self._request("POST", "/activity", data=activity)
        except PanelRemoteError as error:
            logger.warning("Failed to send activity logs to Panel: %s", error)


