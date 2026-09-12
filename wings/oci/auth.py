"""OCI Registry HTTP authentication manager (Bearer & Basic)."""

import base64
from dataclasses import dataclass
import json
import logging
import re
import time
from urllib.error import HTTPError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen


logger = logging.getLogger("wings.oci.auth")

CHALLENGE_RE = re.compile(r'(\w+)="([^"]*)"')


@dataclass
class BearerToken:
    token: str
    expires_at: float


class RegistryAuthManager:
    """Manages authentication tokens for OCI/Docker registries."""

    def __init__(self, credentials: dict[str, tuple[str, str]] | None = None) -> None:
        # Key: registry_host -> (username, password)
        self._credentials: dict[str, tuple[str, str]] = credentials or {}
        # Key: (registry, repository, scope) -> BearerToken
        self._token_cache: dict[tuple[str, str, str], BearerToken] = {}

    def set_credential(self, registry: str, username: str, password: str) -> None:
        """Register credentials for a registry without logging secrets."""
        self._credentials[registry.lower()] = (username, password)

    def parse_challenge(self, header_value: str) -> tuple[str, dict[str, str]]:
        """Parse WWW-Authenticate header into auth type and parameters."""
        if not header_value:
            return "", {}

        parts = header_value.strip().split(" ", 1)
        auth_type = parts[0]
        params: dict[str, str] = {}
        if len(parts) > 1:
            for match in CHALLENGE_RE.finditer(parts[1]):
                params[match.group(1)] = match.group(2)
        return auth_type, params

    def get_auth_header(
        self,
        registry: str,
        repository: str,
        challenge_header: str | None = None,
        scope: str = "pull",
    ) -> dict[str, str]:
        """Generate Authorization headers needed for a request."""
        reg_key = registry.lower()
        full_scope = f"repository:{repository}:{scope}"
        cache_key = (reg_key, repository, full_scope)

        # Check existing cached token
        cached = self._token_cache.get(cache_key)
        if cached and cached.expires_at > time.time() + 60:
            return {"Authorization": f"Bearer {cached.token}"}

        # If credentials exist and no challenge, send basic or fetch token
        creds = self._credentials.get(reg_key)

        if not challenge_header:
            if creds:
                encoded = base64.b64encode(f"{creds[0]}:{creds[1]}".encode()).decode()
                return {"Authorization": f"Basic {encoded}"}
            return {}

        auth_type, params = self.parse_challenge(challenge_header)
        if auth_type.lower() == "basic":
            if creds:
                encoded = base64.b64encode(f"{creds[0]}:{creds[1]}".encode()).decode()
                return {"Authorization": f"Basic {encoded}"}
            return {}

        if auth_type.lower() == "bearer":
            realm = params.get("realm")
            if not realm:
                return {}

            service = params.get("service", "")
            req_scope = params.get("scope", full_scope)

            token = self._fetch_bearer_token(realm, service, req_scope, creds)
            if token:
                # Default 5 minutes if no expires_in provided
                self._token_cache[cache_key] = BearerToken(token=token, expires_at=time.time() + 300)
                return {"Authorization": f"Bearer {token}"}

        return {}

    def _fetch_bearer_token(
        self,
        realm: str,
        service: str,
        scope: str,
        creds: tuple[str, str] | None,
    ) -> str | None:
        """Fetch a bearer token from the token endpoint."""
        query: dict[str, str] = {}
        if service:
            query["service"] = service
        if scope:
            query["scope"] = scope

        token_url = f"{realm}?{urlencode(query)}" if query else realm
        headers = {"User-Agent": "pywings-oci/1.0"}

        if creds:
            encoded = base64.b64encode(f"{creds[0]}:{creds[1]}".encode()).decode()
            headers["Authorization"] = f"Basic {encoded}"

        try:
            req = Request(token_url, headers=headers)
            with urlopen(req, timeout=15) as res:
                data = json.loads(res.read().decode("utf-8"))
                return data.get("token") or data.get("access_token")
        except Exception as err:
            logger.warning("Failed to obtain Bearer token from %s: %s", realm, err)
            return None
