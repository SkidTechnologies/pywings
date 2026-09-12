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

        # 1. Path traversal security check
        parts = Path(norm_name).parts
        if ".." in parts:
            raise ExtractionSecurityError(f"Path traversal detected in layer member: {member.name}")

        dest_path = self.target_rootfs / norm_name

        try:
            dest_abs = os.path.abspath(dest_path)
            rootfs_abs = os.path.abspath(self.target_rootfs)
            if not (dest_abs == rootfs_abs or dest_abs.startswith(rootfs_abs + os.sep)):
                raise ExtractionSecurityError(f"Path traversal detected in layer member: {member.name}")
        except Exception as err:
            if isinstance(err, ExtractionSecurityError):
                raise
            raise ExtractionSecurityError(f"Invalid member path: {member.name}") from err

        # Skip block and character device nodes
        if member.isblk() or member.ischr() or member.isfifo():
            logger.debug("Skipping special device node: %s", member.name)
            return

        # 2. Directory members
        if member.isdir():
            # If dest_path is a symlink to another directory (e.g. /bin -> usr/bin), keep symlink!
            if os.path.islink(dest_path):
                return
            dest_path.mkdir(parents=True, exist_ok=True)
            try:
                dest_path.chmod(member.mode | stat.S_IRWXU)
            except OSError:
                pass
            return

        # Ensure parent directory exists
        parent = dest_path.parent
        if not parent.exists() and not os.path.islink(parent):
            parent.mkdir(parents=True, exist_ok=True)

        # 3. Regular files
        if member.isreg():
            if os.path.islink(dest_path) or os.path.exists(dest_path):
                if os.path.isdir(dest_path) and not os.path.islink(dest_path):
                    shutil.rmtree(dest_path, ignore_errors=True)
                else:
                    try:
                        os.unlink(dest_path)
                    except OSError:
                        pass

            try:
                with tar.extractfile(member) as source:
                    if source:
                        with open(dest_path, "wb") as target:
                            shutil.copyfileobj(source, target)
            except Exception as err:
                logger.warning("Could not extract file %s: %s", dest_path, err)
                return

            try:
                mode = member.mode | stat.S_IRUSR | stat.S_IWUSR
                dest_path.chmod(mode)
            except OSError:
                pass
            return

        # 4. Symbolic links
        if member.issym():
            if os.path.islink(dest_path) or os.path.exists(dest_path):
                try:
                    os.unlink(dest_path)
                except OSError:
                    pass

            link_target = member.linkname
            # If target is absolute (e.g. /bin/busybox or /usr/lib/libcurl.so.4),
            # convert to a relative symlink within the rootfs so it works consistently
            # both inside PRoot and outside on the host filesystem!
            if link_target.startswith("/"):
                target_in_rootfs = self.target_rootfs / link_target.lstrip("/\\")
                try:
                    rel_target = os.path.relpath(target_in_rootfs, dest_path.parent)
                    link_target = rel_target.replace("\\", "/")
                except ValueError:
                    pass
            else:
                target_in_rootfs = dest_path.parent / link_target
                target_abs = os.path.abspath(target_in_rootfs)
                if not (target_abs == rootfs_abs or target_abs.startswith(rootfs_abs + os.sep)):
                    raise ExtractionSecurityError(f"Symlink traversal detected: {member.name} -> {member.linkname}")

            try:
                os.symlink(link_target, dest_path)
            except OSError as err:
                logger.debug("Could not create symlink %s -> %s: %s", dest_path, link_target, err)
                # On Windows without Developer Mode, fallback to file copy if target exists
                resolved_target = dest_path.parent / link_target
                if resolved_target.is_file():
                    try:
                        shutil.copy2(resolved_target, dest_path)
                    except OSError:
                        pass
            return

        # 5. Hard links
        if member.islnk():
            src_norm = member.linkname.lstrip("/\\")
            src_path = self.target_rootfs / src_norm
            if os.path.islink(dest_path) or os.path.exists(dest_path):
                try:
                    os.unlink(dest_path)
                except OSError:
                    pass
            try:
                os.link(src_path, dest_path)
            except OSError:
                if src_path.is_file():
                    try:
                        shutil.copy2(src_path, dest_path)
                    except OSError as err:
                        logger.debug("Could not copy hardlink target %s -> %s: %s", src_path, dest_path, err)
            return
