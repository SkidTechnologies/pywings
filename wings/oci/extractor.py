"""Safe OCI layer tar extractor with AUFS / OCI whiteout semantics."""

import logging
import os
from pathlib import Path
import shutil
import stat
import tarfile


logger = logging.getLogger("wings.oci.extractor")

WHITEOUT_PREFIX = ".wh."
WHITEOUT_OPAQUE = ".wh..wh..opq"


class ExtractionSecurityError(Exception):
    """Raised when an untrusted layer tar attempts directory traversal or dangerous operations."""


class SafeLayerExtractor:
    """Extracts tar layer archives sequentially applying OCI whiteout and security rules."""

    def __init__(self, target_rootfs: Path | str) -> None:
        self.target_rootfs = Path(target_rootfs).resolve()
        self.target_rootfs.mkdir(parents=True, exist_ok=True)

    def extract_layer(self, layer_tar_path: Path | str) -> None:
        """Extract a single layer tarball."""
        self._extract_single_layer(Path(layer_tar_path))

    def extract_layers(self, layer_tar_paths: list[Path | str]) -> None:
        """Extract a sequence of layer tarballs in order (bottom to top)."""
        for idx, layer_path in enumerate(layer_tar_paths):
            p = Path(layer_path)
            logger.debug("Applying OCI layer %d/%d: %s", idx + 1, len(layer_tar_paths), p.name)
            self._extract_single_layer(p)

    def _extract_single_layer(self, tar_path: Path) -> None:
        """Extract a single layer tar archive safely handling whiteouts."""
        with tarfile.open(tar_path, mode="r:*") as tar:
            members = tar.getmembers()

            # First pass: identify and apply whiteouts
            regular_members: list[tarfile.TarInfo] = []
            for member in members:
                norm_name = member.name.lstrip("/\\")
                basename = os.path.basename(norm_name)
                parent_dir = os.path.dirname(norm_name)

                # Check for opaque whiteout: clear previous layer files in this directory
                if basename == WHITEOUT_OPAQUE:
                    target_dir = self.target_rootfs / parent_dir
                    if target_dir.is_dir():
                        logger.debug("Applying opaque whiteout to directory: %s", parent_dir)
                        self._clear_directory_contents(target_dir)
                    continue

                # Check for single file whiteout: delete target file
                if basename.startswith(WHITEOUT_PREFIX):
                    deleted_file = basename[len(WHITEOUT_PREFIX):]
                    target_file = self.target_rootfs / parent_dir / deleted_file
                    logger.debug("Applying file whiteout deleting: %s/%s", parent_dir, deleted_file)
                    if target_file.is_dir() and not target_file.is_symlink():
                        shutil.rmtree(target_file, ignore_errors=True)
                    elif target_file.exists() or target_file.is_symlink():
                        target_file.unlink(missing_ok=True)
                    continue

                regular_members.append(member)

            # Second pass: extract sanitized regular files and directories
            for member in regular_members:
                self._extract_member_safe(tar, member)

    def _clear_directory_contents(self, directory: Path) -> None:
        """Remove all files and subdirectories inside directory without removing directory itself."""
        for item in directory.iterdir():
            if item.is_dir() and not item.is_symlink():
                shutil.rmtree(item, ignore_errors=True)
            else:
                item.unlink(missing_ok=True)

    def _extract_member_safe(self, tar: tarfile.TarFile, member: tarfile.TarInfo) -> None:
        """Validate and extract an individual tar member."""
        norm_name = member.name.lstrip("/\\")
        if not norm_name or norm_name == ".":
            return

        dest_path = (self.target_rootfs / norm_name).resolve()

        # Path traversal validation
        if dest_path != self.target_rootfs and self.target_rootfs not in dest_path.parents:
            raise ExtractionSecurityError(f"Path traversal detected in layer member: {member.name}")

        # Skip block and character device nodes
        if member.isblk() or member.ischr() or member.isfifo():
            logger.debug("Skipping special device node: %s", member.name)
            return

        # Ensure parent directory exists and is a real directory (not an escaping symlink)
        parent = dest_path.parent
        if parent != self.target_rootfs:
            if parent.is_symlink():
                real_parent = parent.resolve()
                if self.target_rootfs not in real_parent.parents and real_parent != self.target_rootfs:
                    raise ExtractionSecurityError(f"Symlink traversal parent detected: {member.name}")
            parent.mkdir(parents=True, exist_ok=True)

        if member.isdir():
            dest_path.mkdir(parents=True, exist_ok=True)
            try:
                dest_path.chmod(member.mode | stat.S_IRWXU)
            except OSError:
                pass
            return

        if member.isreg():
            # If target previously existed as directory, remove it
            if dest_path.is_dir() and not dest_path.is_symlink():
                shutil.rmtree(dest_path, ignore_errors=True)
            elif dest_path.exists() or dest_path.is_symlink():
                dest_path.unlink(missing_ok=True)

            with tar.extractfile(member) as source, open(dest_path, "wb") as target:
                if source:
                    shutil.copyfileobj(source, target)

            try:
                # Ensure owner can read/write and preserve execute bit if set in image
                mode = member.mode | stat.S_IRUSR | stat.S_IWUSR
                dest_path.chmod(mode)
            except OSError:
                pass
            return

        if member.issym():
            dest_path.unlink(missing_ok=True)
            # Normalize target if it points to rootfs
            link_target = member.linkname
            try:
                os.symlink(link_target, dest_path)
            except OSError as err:
                logger.debug("Could not create symlink %s -> %s: %s", dest_path, link_target, err)
            return

        if member.islnk():
            # Hardlink within rootfs
            src_norm = member.linkname.lstrip("/\\")
            src_path = (self.target_rootfs / src_norm).resolve()
            if self.target_rootfs not in src_path.parents and src_path != self.target_rootfs:
                raise ExtractionSecurityError(f"Hardlink traversal detected: {member.name} -> {member.linkname}")
            dest_path.unlink(missing_ok=True)
            try:
                os.link(src_path, dest_path)
            except OSError:
                # Fallback to file copy if hard link is not supported across devices
                if src_path.is_file():
                    shutil.copy2(src_path, dest_path)
