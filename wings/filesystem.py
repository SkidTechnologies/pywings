"""Safe filesystem operations rooted inside one server data directory."""

from datetime import datetime, timezone
import mimetypes
from pathlib import Path
import shutil
import tarfile
import zipfile
import uuid


class FilesystemError(Exception):
    """A filesystem operation could not be completed safely."""


class ServerFilesystem:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, value: str | None = "/") -> Path:
        relative = (value or "/").lstrip("/\\")
        target = (self.root / relative).resolve()
        if target != self.root and self.root not in target.parents:
            raise FilesystemError("The requested path resolves outside the server root.")
        return target

    def stat(self, target: Path) -> dict:
        try:
            info = target.stat()
        except FileNotFoundError as error:
            raise FilesystemError("The requested resource was not found on the system.") from error
        is_directory = target.is_dir()
        return {
            "name": target.name or "/",
            "created": datetime.fromtimestamp(info.st_ctime, timezone.utc).isoformat().replace("+00:00", "Z"),
            "modified": datetime.fromtimestamp(info.st_mtime, timezone.utc).isoformat().replace("+00:00", "Z"),
            "mode": "d---------" if is_directory else "----------",
            "mode_bits": format(info.st_mode & 0o777, "o"),
            "size": 0 if is_directory else info.st_size,
            "directory": is_directory,
            "file": not is_directory,
            "symlink": target.is_symlink(),
            "mime": "inode/directory" if is_directory else (mimetypes.guess_type(target.name)[0] or "application/octet-stream"),
        }

    def list_directory(self, directory: str) -> list[dict]:
        target = self.path(directory)
        if not target.is_dir():
            raise FilesystemError("The requested directory does not exist.")
        entries = [self.stat(entry) for entry in target.iterdir()]
        return sorted(entries, key=lambda item: (not item["directory"], item["name"].lower()))

    def read(self, filename: str) -> tuple[Path, dict]:
        target = self.path(filename)
        if not target.is_file():
            raise FilesystemError("The requested resource was not found on the system.")
        return target, self.stat(target)

    def write(self, filename: str, content: bytes) -> None:
        target = self.path(filename)
        if target.exists() and target.is_dir():
            raise FilesystemError("Cannot write file, name conflicts with an existing directory by the same name.")
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            try:
                target.chmod(0o666)
            except OSError:
                pass
        target.write_bytes(content)

    def upload(self, directory: str, uploads) -> list[dict]:
        """Store multipart uploads below the server root and return file stats."""
        destination = self.path(directory)
        if not destination.is_dir():
            raise FilesystemError("The requested upload directory does not exist.")
        saved = []
        for upload in uploads:
            filename = Path(upload.filename or "").name
            if not filename or filename in {".", ".."}:
                raise FilesystemError("An uploaded file did not have a valid name.")
            target = self.path(str(destination.relative_to(self.root) / filename))
            upload.save(target)
            saved.append(self.stat(target))
        if not saved:
            raise FilesystemError("No files were provided for upload.")
        return saved

    def rename(self, root: str, files: list[dict]) -> None:
        if not files:
            raise FilesystemError("No files to move or rename were provided.")
        base = self.path(root)
        for item in files:
            source = self.path(str(base.relative_to(self.root) / item["from"]))
            destination = self.path(str(base.relative_to(self.root) / item["to"]))
            if not source.exists():
                raise FilesystemError("The requested resource was not found on the system.")
            destination.parent.mkdir(parents=True, exist_ok=True)
            source.replace(destination)

    def copy(self, location: str) -> None:
        source = self.path(location)
        if not source.is_file():
            raise FilesystemError("The requested resource was not found on the system.")
        destination = source.with_name(f"{source.stem} copy{source.suffix}")
        counter = 2
        while destination.exists():
            destination = source.with_name(f"{source.stem} copy {counter}{source.suffix}")
            counter += 1
        shutil.copy2(source, destination)

    def delete(self, root: str, files: list[str]) -> None:
        if not files:
            raise FilesystemError("No files were specified for deletion.")
        base = self.path(root)
        for filename in files:
            target = self.path(str(base.relative_to(self.root) / filename))
            if target == self.root:
                raise FilesystemError("The server root directory cannot be deleted.")
            if target.exists():
                if target.is_dir():
                    shutil.rmtree(target)
                else:
                    target.unlink()

    def create_directory(self, name: str, directory: str) -> None:
        if not name:
            raise FilesystemError("A directory name is required.")
        self.path(str(self.path(directory).relative_to(self.root) / name)).mkdir(parents=True, exist_ok=True)

    def chmod(self, root: str, files: list[dict]) -> None:
        if not files:
            raise FilesystemError("No files to chmod were provided.")
        base = self.path(root)
        for item in files:
            try:
                mode = int(str(item["mode"]), 8)
                target = self.path(str(base.relative_to(self.root) / item["file"]))
                target.chmod(mode)
            except (KeyError, ValueError) as error:
                raise FilesystemError("Invalid file mode.") from error

    def compress(self, root: str, files: list[str], archive_name: str | None = None, archive_type: str = "tar.gz") -> dict:
        if not files:
            raise FilesystemError("No files were passed through to be compressed.")
        base = self.path(root)
        archive_type = archive_type.lower().lstrip(".")
        if archive_type in {"zip"}:
            suffix = ".zip"
            archive = base / (archive_name or f"archive{suffix}")
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as output:
                for filename in files:
                    target = self.path(str(base.relative_to(self.root) / filename))
                    if target.exists():
                        if target.is_dir():
                            for child in target.rglob("*"):
                                if child.is_file():
                                    output.write(child, child.relative_to(base))
                        else:
                            output.write(target, target.name)
        elif archive_type in {"tar", "tar.gz", "tgz"}:
            suffix = ".tar.gz"
            archive = base / (archive_name or f"archive{suffix}")
            mode = "w" if archive_type == "tar" else "w:gz"
            with tarfile.open(archive, mode) as output:
                for filename in files:
                    target = self.path(str(base.relative_to(self.root) / filename))
                    if target.exists():
                        output.add(target, arcname=target.name)
        else:
            raise FilesystemError("The archive type must be tar, tar.gz, tgz, or zip.")
        return self.stat(archive)

    def decompress(self, root: str, filename: str) -> None:
        archive = self.path(filename)
        destination = self.path(root)
        if not archive.is_file():
            raise FilesystemError("The requested resource was not found on the system.")
        if tarfile.is_tarfile(archive):
            with tarfile.open(archive) as source:
                for member in source.getmembers():
                    if member.issym() or member.islnk():
                        raise FilesystemError("The archive contains unsupported symbolic or hard links.")
                    target = (destination / member.name).resolve()
                    if target != self.root and self.root not in target.parents:
                        raise FilesystemError("The archive contains a path outside the server root.")
                source.extractall(destination)
        elif zipfile.is_zipfile(archive):
            with zipfile.ZipFile(archive) as source:
                for member in source.namelist():
                    target = (destination / member).resolve()
                    if target != self.root and self.root not in target.parents:
                        raise FilesystemError("The archive contains a path outside the server root.")
                source.extractall(destination)
        else:
            raise FilesystemError("The archive provided is in a format Wings does not understand.")

    def create_backup(self, backup_id: str | None = None, name: str | None = None) -> dict:
        backup_id = backup_id or str(uuid.uuid4())
        if Path(backup_id).name != backup_id:
            raise FilesystemError("Invalid backup identifier.")
        backup_dir = self.root / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        archive = backup_dir / f"{backup_id}.tar.gz"
        with tarfile.open(archive, "w:gz") as output:
            for entry in self.root.iterdir():
                if entry.name != "backups":
                    output.add(entry, arcname=entry.name)
        stat = self.stat(archive)
        stat.update({"uuid": backup_id, "name": name or archive.name, "successful": True})
        return stat

    def list_backups(self) -> list[dict]:
        backup_dir = self.root / "backups"
        if not backup_dir.exists():
            return []
        result = []
        for archive in sorted(backup_dir.glob("*.tar.gz")):
            item = self.stat(archive)
            item.update({"uuid": archive.name[:-7], "name": archive.name, "successful": True})
            result.append(item)
        return result

    def delete_backup(self, backup_id: str) -> None:
        if Path(backup_id).name != backup_id:
            raise FilesystemError("Invalid backup identifier.")
        archive = self.root / "backups" / f"{backup_id}.tar.gz"
        if not archive.is_file():
            raise FilesystemError("The requested backup was not found.")
        archive.unlink()

    def backup_path(self, backup_id: str) -> Path:
        if Path(backup_id).name != backup_id:
            raise FilesystemError("Invalid backup identifier.")
        archive = self.root / "backups" / f"{backup_id}.tar.gz"
        if not archive.is_file():
            raise FilesystemError("The requested backup was not found.")
        return archive

    def restore_backup(self, backup_id: str) -> None:
        archive = self.backup_path(backup_id)
        with tarfile.open(archive, "r:gz") as source:
            members = source.getmembers()
            for member in members:
                if member.issym() or member.islnk():
                    raise FilesystemError("The backup contains unsupported symbolic or hard links.")
                target = (self.root / member.name).resolve()
                if target == self.root / "backups" or self.root / "backups" in target.parents:
                    raise FilesystemError("The backup cannot restore files into the backups directory.")
                if target != self.root and self.root not in target.parents:
                    raise FilesystemError("The backup contains a path outside the server root.")
            source.extractall(self.root, members=members)
