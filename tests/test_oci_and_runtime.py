"""Unit tests for pywings custom OCI client, rootfs extractor, and PRoot runtime."""

import io
import os
from pathlib import Path
import shutil
import tarfile
import tempfile
import unittest

from wings.oci.reference import OciReference
from wings.oci.manifest import ManifestParser, Descriptor, ImageConfig, Manifest
from wings.oci.extractor import SafeLayerExtractor
from wings.runtime.base import RuntimeError, RuntimeUnavailableError
from wings.runtime.proot_detector import ProotDetector
from wings.runtime.proot_runtime import ProotRuntime


class TestOciReference(unittest.TestCase):
    def test_parse_simple_name(self):
        ref = OciReference.parse("ubuntu")
        self.assertEqual(ref.registry, "registry-1.docker.io")
        self.assertEqual(ref.repository, "library/ubuntu")
        self.assertEqual(ref.tag, "latest")
        self.assertIsNone(ref.digest)
        self.assertEqual(ref.normalized_name, "library/ubuntu:latest")

    def test_parse_tagged_image(self):
        ref = OciReference.parse("alpine:3.18")
        self.assertEqual(ref.registry, "registry-1.docker.io")
        self.assertEqual(ref.repository, "library/alpine")
        self.assertEqual(ref.tag, "3.18")

    def test_parse_ghcr_image(self):
        ref = OciReference.parse("ghcr.io/pterodactyl/yolks:java_17")
        self.assertEqual(ref.registry, "ghcr.io")
        self.assertEqual(ref.repository, "pterodactyl/yolks")
        self.assertEqual(ref.tag, "java_17")
        self.assertIsNone(ref.digest)

    def test_parse_digest(self):
        digest = "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        ref = OciReference.parse(f"python@{digest}")
        self.assertEqual(ref.registry, "registry-1.docker.io")
        self.assertEqual(ref.repository, "library/python")
        self.assertEqual(ref.digest, digest)

    def test_parse_custom_registry_port(self):
        ref = OciReference.parse("localhost:5000/myorg/myimage:v1")
        self.assertEqual(ref.registry, "localhost:5000")
        self.assertEqual(ref.repository, "myorg/myimage")
        self.assertEqual(ref.tag, "v1")


class TestOciManifest(unittest.TestCase):
    def test_parse_single_manifest(self):
        raw = {
            "schemaVersion": 2,
            "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
            "config": {
                "mediaType": "application/vnd.docker.container.image.v1+json",
                "size": 1234,
                "digest": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            },
            "layers": [
                {
                    "mediaType": "application/vnd.docker.image.rootfs.diff.tar.gzip",
                    "size": 5678,
                    "digest": "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                }
            ],
        }
        manifest = ManifestParser.parse(raw)
        self.assertIsInstance(manifest, Manifest)
        self.assertEqual(len(manifest.layers), 1)
        self.assertEqual(manifest.layers[0].digest, "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")

    def test_resolve_manifest_list_platform(self):
        manifest_list = {
            "schemaVersion": 2,
            "mediaType": "application/vnd.docker.distribution.manifest.list.v2+json",
            "manifests": [
                {
                    "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
                    "size": 1000,
                    "digest": "sha256:1111111111111111111111111111111111111111111111111111111111111111",
                    "platform": {"architecture": "arm64", "os": "linux"},
                },
                {
                    "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
                    "size": 2000,
                    "digest": "sha256:2222222222222222222222222222222222222222222222222222222222222222",
                    "platform": {"architecture": "amd64", "os": "linux"},
                },
            ],
        }
        descriptor = ManifestParser.resolve_platform(manifest_list, target_arch="amd64", target_os="linux")
        self.assertIsNotNone(descriptor)
        self.assertEqual(descriptor.digest, "sha256:2222222222222222222222222222222222222222222222222222222222222222")


class TestSafeLayerExtractor(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="pywings_test_rootfs_")
        self.rootfs = Path(self.test_dir) / "rootfs"
        self.rootfs.mkdir(parents=True, exist_ok=True)
        self.extractor = SafeLayerExtractor(self.rootfs)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _create_tar(self, files_dict: dict[str, bytes]) -> Path:
        tar_path = Path(self.test_dir) / f"layer_{len(files_dict)}.tar"
        with tarfile.open(tar_path, "w") as tar:
            for name, content in files_dict.items():
                ti = tarfile.TarInfo(name=name)
                ti.size = len(content)
                ti.mtime = 1000
                ti.mode = 0o644
                tar.addfile(ti, io.BytesIO(content))
        return tar_path

    def test_safe_file_extraction(self):
        tar_path = self._create_tar({"etc/hosts": b"127.0.0.1 localhost\n"})
        self.extractor.extract_layer(tar_path)
        extracted = self.rootfs / "etc" / "hosts"
        self.assertTrue(extracted.exists())
        self.assertEqual(extracted.read_bytes(), b"127.0.0.1 localhost\n")

    def test_whiteout_file_deletion(self):
        # Layer 1: create file
        tar1 = self._create_tar({"etc/test.txt": b"hello"})
        self.extractor.extract_layer(tar1)
        self.assertTrue((self.rootfs / "etc" / "test.txt").exists())

        # Layer 2: whiteout file .wh.test.txt
        tar2 = self._create_tar({"etc/.wh.test.txt": b""})
        self.extractor.extract_layer(tar2)
        self.assertFalse((self.rootfs / "etc" / "test.txt").exists())
        self.assertFalse((self.rootfs / "etc" / ".wh.test.txt").exists())

    def test_opaque_whiteout_cleans_directory(self):
        # Layer 1: populate directory
        tar1 = self._create_tar({
            "opt/app/one.txt": b"1",
            "opt/app/two.txt": b"2",
        })
        self.extractor.extract_layer(tar1)
        self.assertTrue((self.rootfs / "opt" / "app" / "one.txt").exists())
        self.assertTrue((self.rootfs / "opt" / "app" / "two.txt").exists())

        # Layer 2: opaque whiteout in opt/app
        tar2 = self._create_tar({
            "opt/app/.wh..wh..opq": b"",
            "opt/app/three.txt": b"3",
        })
        self.extractor.extract_layer(tar2)
        self.assertFalse((self.rootfs / "opt" / "app" / "one.txt").exists())
        self.assertFalse((self.rootfs / "opt" / "app" / "two.txt").exists())
        self.assertTrue((self.rootfs / "opt" / "app" / "three.txt").exists())


class TestProotRuntime(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="pywings_test_proot_")
        self.runtime_dir = Path(self.test_dir) / "runtime"
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        # Point to dummy or system proot
        self.runtime = ProotRuntime(data_directory=self.runtime_dir, proot_path="/fake/proot")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_build_proot_cmd_root_jail_and_bindings(self):
        rootfs = self.runtime_dir / "containers" / "srv1" / "rootfs"
        rootfs.mkdir(parents=True, exist_ok=True)
        server_data = Path(self.test_dir) / "server_data"
        server_data.mkdir(parents=True, exist_ok=True)

        cmd = self.runtime.build_proot_cmd(
            rootfs=rootfs,
            command=["/bin/sh", "-c", "echo hello"],
            workdir="/home/container",
            volumes=[f"{server_data}:/home/container"],
        )

        self.assertIn("/fake/proot", cmd[0])
        self.assertIn("-n", cmd)
        self.assertIn("-0", cmd)
        self.assertIn("-r", cmd)
        rootfs_idx = cmd.index("-r") + 1
        self.assertEqual(Path(cmd[rootfs_idx]).resolve(), rootfs.resolve())
        self.assertIn("-w", cmd)
        workdir_idx = cmd.index("-w") + 1
        self.assertEqual(cmd[workdir_idx], "/home/container")
        self.assertIn("-b", cmd)
        # Ensure command is placed at the end
        self.assertEqual(cmd[-3:], ["/bin/sh", "-c", "echo hello"])

    def test_unsafe_mount_rejected(self):
        rootfs = self.runtime_dir / "containers" / "srv1" / "rootfs"
        rootfs.mkdir(parents=True, exist_ok=True)

        # Attempting to mount host / or /etc to /inside should be rejected by validate_safe_bindings
        with self.assertRaises(RuntimeError):
            self.runtime.validate_safe_bindings([
                "/etc:/etc"
            ])


class TestRegistryAuth(unittest.TestCase):
    def test_parse_bearer_challenge(self):
        from wings.oci.auth import RegistryAuthManager

        manager = RegistryAuthManager()
        header = 'Bearer realm="https://auth.docker.io/token",service="registry.docker.io",scope="repository:library/ubuntu:pull"'
        auth_type, params = manager.parse_challenge(header)
        self.assertEqual(auth_type.lower(), "bearer")
        self.assertEqual(params["realm"], "https://auth.docker.io/token")
        self.assertEqual(params["service"], "registry.docker.io")
        self.assertEqual(params["scope"], "repository:library/ubuntu:pull")


class TestContentAddressableCache(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="pywings_test_cache_")
        from wings.oci.cache import ContentAddressableCache
        self.cache = ContentAddressableCache(self.test_dir)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_store_and_retrieve_blob(self):
        import hashlib
        data = b"hello world layer data"
        digest = f"sha256:{hashlib.sha256(data).hexdigest()}"

        blob_path = self.cache.store_blob(digest, io.BytesIO(data))
        self.assertTrue(blob_path.exists())
        self.assertTrue(self.cache.has_blob(digest))
        self.assertEqual(blob_path.read_bytes(), data)


class TestPathTraversalProtection(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="pywings_test_traversal_")
        self.rootfs = Path(self.test_dir) / "rootfs"
        self.rootfs.mkdir(parents=True, exist_ok=True)
        self.extractor = SafeLayerExtractor(self.rootfs)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_traversal_member_skipped(self):
        from wings.oci.extractor import ExtractionSecurityError
        tar_path = Path(self.test_dir) / "malicious.tar"
        with tarfile.open(tar_path, "w") as tar:
            ti = tarfile.TarInfo(name="../../escaped.txt")
            data = b"escape!"
            ti.size = len(data)
            tar.addfile(ti, io.BytesIO(data))

        with self.assertRaises(ExtractionSecurityError):
            self.extractor.extract_layer(tar_path)

        # Verify the file was NOT created outside target_rootfs
        outside_file = Path(self.test_dir) / "escaped.txt"
        self.assertFalse(outside_file.exists())


class TestFlaskIntegration(unittest.TestCase):
    def test_create_app_with_proot(self):
        from wings import create_app
        from wings.config import Settings

        test_dir = tempfile.mkdtemp(prefix="pywings_app_test_")
        try:
            settings = Settings(
                data_directory=test_dir,
                proot_path="/bin/true",
            )
            app = create_app(settings)
            self.assertIn("container_runtime", app.extensions)
            self.assertIsInstance(app.extensions["container_runtime"], ProotRuntime)
        finally:
            shutil.rmtree(test_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
