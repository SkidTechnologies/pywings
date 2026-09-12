"""OCI Registry HTTP API Client (async with sync helpers)."""

import asyncio
import hashlib
import io
import json
import logging
from typing import AsyncIterator, Callable, Iterator
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from wings.oci.auth import RegistryAuthManager
from wings.oci.manifest import (
    ALL_ACCEPTED_MANIFEST_TYPES,
    Descriptor,
    Manifest,
    ManifestParser,
    get_default_platform,
)
from wings.oci.reference import OciReference


logger = logging.getLogger("wings.oci.client")


class OciRegistryError(Exception):
    """Raised when an OCI registry request fails."""


class OciRegistryClient:
    """HTTP client communicating with OCI and Docker Registry V2 APIs."""

    def __init__(
        self,
        auth_manager: RegistryAuthManager | None = None,
        timeout: int = 30,
        insecure: bool = False,
    ) -> None:
        self.auth_manager = auth_manager or RegistryAuthManager()
        self.timeout = timeout
        self.insecure = insecure

    def _base_url(self, registry: str) -> str:
        scheme = "http" if self.insecure or registry.startswith("localhost") else "https"
        return f"{scheme}://{registry}/v2"

    def _request_with_auth(
        self,
        method: str,
        url: str,
        ref: OciReference,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        """Execute request, handling 401 WWW-Authenticate challenge if needed."""
        req_headers = dict(headers or {})
        req_headers.setdefault("User-Agent", "pywings-oci/1.0")

        # Check existing cached token
        auth_hdr = self.auth_manager.get_auth_header(ref.registry, ref.repository)
        req_headers.update(auth_hdr)

        req = Request(url, headers=req_headers, method=method)
        try:
            with urlopen(req, timeout=self.timeout) as resp:
                data = resp.read()
                resp_headers = {k.lower(): v for k, v in resp.headers.items()}
                return resp.status, resp_headers, data
        except HTTPError as err:
            if err.code == 401:
                # Parse challenge and retry
                challenge = err.headers.get("Www-Authenticate")
                retry_auth = self.auth_manager.get_auth_header(
                    ref.registry, ref.repository, challenge_header=challenge
                )
                if retry_auth:
                    req_headers.update(retry_auth)
                    retry_req = Request(url, headers=req_headers, method=method)
                    try:
                        with urlopen(retry_req, timeout=self.timeout) as resp:
                            data = resp.read()
                            resp_headers = {k.lower(): v for k, v in resp.headers.items()}
                            return resp.status, resp_headers, data
                    except HTTPError as r_err:
                        body = r_err.read().decode("utf-8", errors="replace")
                        raise OciRegistryError(
                            f"Registry request {method} {url} failed ({r_err.code}): {body}"
                        ) from r_err

            body = err.read().decode("utf-8", errors="replace")
            raise OciRegistryError(f"Registry request {method} {url} failed ({err.code}): {body}") from err

    def get_manifest(
        self,
        ref: OciReference,
        target_os: str | None = None,
        target_arch: str | None = None,
    ) -> tuple[Manifest, str]:
        """Fetch and resolve image manifest for target platform.

        Returns (Manifest, manifest_digest_or_tag).
        """
        host_os, host_arch = get_default_platform()
        req_os = target_os or host_os
        req_arch = target_arch or host_arch

        url = f"{self._base_url(ref.registry)}/{ref.repository}/manifests/{ref.reference}"
        headers = {"Accept": ALL_ACCEPTED_MANIFEST_TYPES}

        status, resp_headers, data = self._request_with_auth("GET", url, ref, headers)
        manifest_json = json.loads(data.decode("utf-8"))

        manifest_digest = resp_headers.get("docker-content-digest")
        if not manifest_digest:
            manifest_digest = f"sha256:{hashlib.sha256(data).hexdigest()}"

        # If it's an index or manifest list, resolve platform
        if ManifestParser.is_index(manifest_json):
            desc = ManifestParser.resolve_platform_manifest(
                manifest_json, target_os=req_os, target_arch=req_arch
            )
            logger.info("Resolved multi-arch manifest list to %s (%s)", desc.digest, desc.platform)
            # Fetch resolved platform manifest
            platform_url = f"{self._base_url(ref.registry)}/{ref.repository}/manifests/{desc.digest}"
            _, p_headers, p_data = self._request_with_auth("GET", platform_url, ref, {"Accept": desc.media_type})
            p_json = json.loads(p_data.decode("utf-8"))
            return ManifestParser.parse_image_manifest(p_json), desc.digest

        return ManifestParser.parse_image_manifest(manifest_json), manifest_digest

    def get_config_json(self, ref: OciReference, config_digest: str) -> dict:
        """Fetch container image configuration JSON blob."""
        url = f"{self._base_url(ref.registry)}/{ref.repository}/blobs/{config_digest}"
        _, _, data = self._request_with_auth("GET", url, ref)
        # Verify digest
        expected = config_digest.split(":", 1)[1] if ":" in config_digest else config_digest
        actual = hashlib.sha256(data).hexdigest()
        if actual != expected:
            raise OciRegistryError(f"Config digest mismatch: expected {expected}, got {actual}")
        return json.loads(data.decode("utf-8"))

    def download_blob_to_file(
        self,
        ref: OciReference,
        digest: str,
        destination: str,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> None:
        """Download layer blob directly to destination path with streaming SHA256 validation."""
        url = f"{self._base_url(ref.registry)}/{ref.repository}/blobs/{digest}"
        auth_hdr = self.auth_manager.get_auth_header(ref.registry, ref.repository)
        req_headers = {"User-Agent": "pywings-oci/1.0"}
        req_headers.update(auth_hdr)

        expected_hash = digest.split(":", 1)[1] if ":" in digest else digest
        hasher = hashlib.sha256()

        temp_path = f"{destination}.tmp"
        req = Request(url, headers=req_headers)
        try:
            with urlopen(req, timeout=self.timeout) as resp:
                total_size = int(resp.headers.get("Content-Length", 0))
                downloaded = 0
                with open(temp_path, "wb") as f:
                    while chunk := resp.read(1024 * 1024):  # 1MB chunks
                        hasher.update(chunk)
                        f.write(chunk)
                        downloaded += len(chunk)
                        if progress_callback and total_size:
                            progress_callback(downloaded, total_size)
        except HTTPError as err:
            if err.code == 401:
                challenge = err.headers.get("Www-Authenticate")
                retry_auth = self.auth_manager.get_auth_header(
                    ref.registry, ref.repository, challenge_header=challenge
                )
                req_headers.update(retry_auth)
                hasher = hashlib.sha256()
                retry_req = Request(url, headers=req_headers)
                with urlopen(retry_req, timeout=self.timeout) as resp:
                    total_size = int(resp.headers.get("Content-Length", 0))
                    downloaded = 0
                    with open(temp_path, "wb") as f:
                        while chunk := resp.read(1024 * 1024):
                            hasher.update(chunk)
                            f.write(chunk)
                            downloaded += len(chunk)
                            if progress_callback and total_size:
                                progress_callback(downloaded, total_size)
            else:
                raise

        actual_hash = hasher.hexdigest()
        if actual_hash != expected_hash:
            import os
            try:
                os.unlink(temp_path)
            except OSError:
                pass
            raise OciRegistryError(f"Layer blob digest mismatch for {digest}: got {actual_hash}")

        import os
        os.replace(temp_path, destination)
