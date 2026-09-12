"""OCI / Docker registry, manifest, layer, and rootfs management package."""

from wings.oci.auth import RegistryAuthManager
from wings.oci.cache import ContentAddressableCache
from wings.oci.client import OciRegistryClient, OciRegistryError
from wings.oci.extractor import ExtractionSecurityError, SafeLayerExtractor
from wings.oci.image import ImageInstance, OciImageManager
from wings.oci.manifest import Descriptor, ImageConfig, Manifest, ManifestParser
from wings.oci.reference import OciReference

__all__ = [
    "OciReference",
    "RegistryAuthManager",
    "OciRegistryClient",
    "OciRegistryError",
    "ManifestParser",
    "Manifest",
    "Descriptor",
    "ImageConfig",
    "ContentAddressableCache",
    "SafeLayerExtractor",
    "ExtractionSecurityError",
    "OciImageManager",
    "ImageInstance",
]
