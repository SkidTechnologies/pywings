"""HTTP API exposed by Wings.

The route list intentionally follows the original Wings router. Runtime
container-specific details will be added with the udocker adapter.
"""

from functools import wraps
import http.client
import json
import logging
import os
from pathlib import Path
import platform
from queue import Empty
from threading import Lock, Thread
import time
from urllib.parse import urlparse
from urllib.request import Request, urlopen
import uuid

from flask import Blueprint, current_app, jsonify, request, send_file
import jwt

from wings.events import bus
from wings.servers import ServerRecord, valid_server_uuid
from wings.runtime import RuntimeCommandError, RuntimeUnavailableError
from wings.remote import PanelRemoteError
from wings.filesystem import FilesystemError, ServerFilesystem


logger = logging.getLogger("wings.api")


api = Blueprint("api", __name__)
_used_file_tokens: set[str] = set()
_used_file_tokens_lock = Lock()
_denied_websocket_jtis: set[str] = set()
_denied_websocket_jtis_lock = Lock()
_remote_downloads: dict[str, dict] = {}
_remote_downloads_lock = Lock()
_transfers: dict[str, dict] = {}
_transfers_lock = Lock()


def require_authorization(view):
    """Match Wings' Bearer-header validation for protected routes."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if request.method == "OPTIONS":
            return view(*args, **kwargs)
        auth = request.headers.get("Authorization", "").split(" ", 1)
        if len(auth) != 2 or auth[0] != "Bearer":
            response = jsonify({
                "error": "The required authorization heads were not present in the request."
            })
            response.status_code = 401
            response.headers["WWW-Authenticate"] = "Bearer"
            return response

        expected = current_app.config.get("TOKEN", "")
        if not expected or auth[1] != expected:
            return jsonify({"error": "You are not authorized to access this endpoint."}), 403

        # Authenticated Panel request received -> capture client IP as fallback in case configured remote is unreachable
        remote_ip = request.headers.get("X-Forwarded-For", request.remote_addr)
        if remote_ip and "remote_client" in current_app.extensions:
            current_app.extensions["remote_client"].add_fallback_host(remote_ip)

        return view(*args, **kwargs)

    return wrapped


def _decode_file_token(raw_token: str, scope: str = "file-download") -> dict:
    claims = jwt.decode(
        raw_token,
        current_app.config["TOKEN"],
        algorithms=["HS256"],
        options={"verify_aud": False, "verify_iat": False, "verify_nbf": False},
    )
    if claims.get("scope") != scope:
        raise ValueError(f"token does not have {scope} scope")
    unique_id = claims.get("unique_id")
    if not unique_id:
        raise ValueError("file token has no unique_id")
    with _used_file_tokens_lock:
        if unique_id in _used_file_tokens:
            raise ValueError("file token has already been used")
        _used_file_tokens.add(unique_id)
    return claims


@api.get("/download/file")
def download_file():
    """Serve a single file using the signed one-time token used by Panel."""
    try:
        claims = _decode_file_token(request.args.get("token", ""), "file-download")
        server_uuid = claims.get("server_uuid", "")
        server, error = _require_server(server_uuid)
        if error:
            return error
        target, stat = _filesystem(server_uuid).read(claims.get("file_path", ""))
        if stat["directory"]:
            return jsonify({"error": "The requested resource was not found on this server."}), 404
        return send_file(
            target,
            as_attachment=True,
            download_name=target.name,
            mimetype="application/octet-stream",
        )
    except (jwt.InvalidTokenError, ValueError, FilesystemError):
        return jsonify({"error": "The requested resource was not found on this server."}), 404


@api.get("/download/backup")
def download_backup_signed():
    try:
        claims = _decode_file_token(request.args.get("token", ""), "backup-download")
        server_uuid = claims.get("server_uuid", "")
        _server, error = _require_server(server_uuid)
        if error:
            return error
        backup_id = claims.get("backup_uuid") or claims.get("backup_id")
        archive = _filesystem(server_uuid).backup_path(str(backup_id))
        return send_file(archive, as_attachment=True, download_name=archive.name, mimetype="application/gzip")
    except (jwt.InvalidTokenError, ValueError, FilesystemError):
        return jsonify({"error": "The requested resource was not found on this server."}), 404


@api.post("/upload/file")
def upload_file_signed():
    try:
        claims = _decode_file_token(request.args.get("token", ""), "file-upload")
        server_uuid = claims.get("server_uuid", "")
        _server, error = _require_server(server_uuid)
        if error:
            return error
        directory = claims.get("directory", request.args.get("directory", "/"))
        files = _filesystem(server_uuid).upload(directory, request.files.values())
        return jsonify(files), 201
    except (jwt.InvalidTokenError, ValueError, FilesystemError):
        return jsonify({"error": "The requested resource was not found on this server."}), 404


def _remote_download_worker(download_id: str, server_uuid: str, url: str, directory: str, filename: str, limit: int) -> None:
    temporary = None
    try:
        target_dir = _filesystem(server_uuid).path(directory)
        if not target_dir.is_dir():
            raise FilesystemError("The requested download directory does not exist.")
        target = _filesystem(server_uuid).path(str(target_dir.relative_to(_filesystem(server_uuid).root) / filename))
        temporary = target.with_name(f".{target.name}.{download_id}.part")
        with urlopen(Request(url, headers={"User-Agent": "pyWings/0.1"}), timeout=30) as response:
            content_length = response.headers.get("Content-Length")
            if content_length and int(content_length) > limit:
                raise ValueError("The remote file exceeds the configured upload limit.")
            received = 0
            with temporary.open("wb") as output:
                while True:
                    with _remote_downloads_lock:
                        if _remote_downloads.get(download_id, {}).get("cancelled"):
                            raise RuntimeError("download cancelled")
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > limit:
                        raise ValueError("The remote file exceeds the configured upload limit.")
                    output.write(chunk)
        temporary.replace(target)
        status = {"uuid": download_id, "server_uuid": server_uuid, "status": "completed", "file": filename}
    except Exception as error:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        status = {"uuid": download_id, "server_uuid": server_uuid, "status": "failed", "error": str(error)}
    with _remote_downloads_lock:
        _remote_downloads[download_id] = status


@api.route("/api/servers/<server_uuid>/files/pull", methods=["GET", "POST", "OPTIONS"], provide_automatic_options=False)
@require_authorization
def remote_file_downloads(server_uuid: str):
    if request.method == "OPTIONS":
        return "", 204
    _, error = _require_server(server_uuid)
    if error:
        return error
    if request.method == "GET":
        with _remote_downloads_lock:
            return jsonify([item for item in _remote_downloads.values() if item.get("server_uuid") == server_uuid])
    payload = request.get_json(silent=True) or {}
    url = str(payload.get("url", ""))
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return jsonify({"error": "A valid HTTP or HTTPS URL is required."}), 400
    filename = Path(str(payload.get("filename") or Path(parsed.path).name)).name
    if not filename or filename in {".", ".."}:
        return jsonify({"error": "A valid filename is required."}), 400
    download_id = str(uuid.uuid4())
    directory = str(payload.get("directory", "/"))
    with _remote_downloads_lock:
        _remote_downloads[download_id] = {
            "uuid": download_id, "server_uuid": server_uuid, "status": "processing", "file": filename,
        }
    limit = int(current_app.config["UPLOAD_LIMIT"]) * 1024 * 1024
    Thread(target=_remote_download_worker, args=(download_id, server_uuid, url, directory, filename, limit), daemon=True).start()
    return jsonify({"uuid": download_id}), 202


@api.route("/api/servers/<server_uuid>/files/pull/<download_id>", methods=["DELETE", "OPTIONS"], provide_automatic_options=False)
@require_authorization
def cancel_remote_download(server_uuid: str, download_id: str):
    if request.method == "OPTIONS":
        return "", 204
    _, error = _require_server(server_uuid)
    if error:
        return error
    with _remote_downloads_lock:
        item = _remote_downloads.get(download_id)
        if item is None or item.get("server_uuid") != server_uuid:
            return jsonify({"error": "The requested download was not found."}), 404
        item["cancelled"] = True
        item["status"] = "cancelled"
    return "", 204


@api.route("/api/system", methods=["GET", "OPTIONS"], provide_automatic_options=False)
@require_authorization
def system_information():
    """Return Wings system information, with an optional runtime v2 view."""
    if request.method == "OPTIONS":
        return "", 204
    response = {
        "architecture": platform.machine(),
        "cpu_count": os.cpu_count() or 1,
        "kernel_version": platform.release(),
        "os": platform.platform(),
        "version": current_app.config["VERSION"],
    }
    if request.args.get("v") == "2":
        runtime = current_app.extensions["process_manager"].runtime
        runtime_info = {"available": True}
        try:
            result = runtime.version()
            runtime_info["version"] = result.stdout.strip() or result.stderr.strip()
        except RuntimeUnavailableError as error:
            runtime_info = {"available": False, "error": str(error)}
        response["runtime"] = runtime_info
        response["data_directory"] = current_app.config["DATA_DIRECTORY"]
        response["server_count"] = len(_server_store().all())
    return jsonify(response)


@api.route("/api/system/update", methods=["POST", "OPTIONS"], provide_automatic_options=False)
@require_authorization
def system_update():
    """Trigger manual update check and in-place daemon restart."""
    if request.method == "OPTIONS":
        return "", 204
    updater = current_app.extensions.get("updater")
    if not updater:
        return jsonify({"error": "Auto-updater not initialized or not in a git repository."}), 400
    has_update, local_sha, remote_sha = updater.check_update()
    if has_update:
        Thread(target=updater.apply_update_and_restart, args=(local_sha, remote_sha), daemon=True).start()
        return jsonify({"updating": True, "local_commit": local_sha, "remote_commit": remote_sha}), 202
    return jsonify({"updating": False, "local_commit": local_sha, "remote_commit": remote_sha}), 200


@api.route("/api/update", methods=["POST", "OPTIONS"], provide_automatic_options=False)
@require_authorization
def update_all_servers():
    if request.method == "OPTIONS":
        return "", 204
    failures = []
    client = current_app.extensions["remote_client"]
    store = _server_store()
    for server in store.all():
        try:
            configuration = client.get_server_configuration(server.uuid)
            store.update_configuration(server.uuid, configuration)
        except PanelRemoteError as error:
            failures.append({"uuid": server.uuid, "error": str(error)})
    if failures:
        return jsonify({"error": "One or more server configurations could not be updated.", "failures": failures}), 502
    return "", 204


@api.route("/api/transfers", methods=["POST", "OPTIONS"], provide_automatic_options=False)
@require_authorization
def receive_transfer():
    if request.method == "OPTIONS":
        return "", 204
    server_uuid = str(request.form.get("server_uuid", ""))
    if not valid_server_uuid(server_uuid):
        return jsonify({"error": "A valid server_uuid is required."}), 422
    upload = request.files.get("archive") or request.files.get("file")
    if upload is None:
        return jsonify({"error": "The transfer archive is required."}), 400
    server_root = Path(current_app.config["DATA_DIRECTORY"]) / server_uuid
    temporary = server_root / "backups" / f"incoming-{uuid.uuid4()}.tar.gz"
    temporary.parent.mkdir(parents=True, exist_ok=True)
    try:
        upload.save(temporary)
        _server_store().ensure(server_uuid)
        filesystem = _filesystem(server_uuid)
        # Restore performs member validation before extracting.
        filesystem.restore_backup(temporary.name[:-7])
        return "", 202
    except (FilesystemError, OSError) as exc:
        return jsonify({"error": str(exc)}), 400
    finally:
        temporary.unlink(missing_ok=True)


@api.route("/api/transfers/<server_uuid>", methods=["DELETE", "OPTIONS"], provide_automatic_options=False)
@require_authorization
def delete_incoming_transfer(server_uuid: str):
    if request.method == "OPTIONS":
        return "", 204
    with _transfers_lock:
        transfer = _transfers.pop(server_uuid, None)
    if transfer:
        try:
            _filesystem(server_uuid).delete_backup(transfer["uuid"])
        except FilesystemError:
            pass
    return "", 204


@api.route("/api/deauthorize-user", methods=["POST", "OPTIONS"], provide_automatic_options=False)
@require_authorization
def deauthorize_user():
    if request.method == "OPTIONS":
        return "", 204
    payload = request.get_json(silent=True) or {}
    jtis = payload.get("jtis") or payload.get("tokens") or []
    if not isinstance(jtis, list):
        return jsonify({"error": "The jtis field must be an array."}), 400
    with _denied_websocket_jtis_lock:
        _denied_websocket_jtis.update(str(jti) for jti in jtis if jti)
    return "", 204


def _server_store():
    return current_app.extensions["server_store"]


def _filesystem(server_uuid: str) -> ServerFilesystem:
    return ServerFilesystem(Path(current_app.config["DATA_DIRECTORY"]) / server_uuid)


@api.route("/api/servers", methods=["GET", "POST", "OPTIONS"], provide_automatic_options=False)
@require_authorization
def servers():
    """List registered servers or register one from the Panel."""
    if request.method == "OPTIONS":
        return "", 204
    if request.method == "GET":
        manager = current_app.extensions["process_manager"]
        return jsonify([
            server.to_api_response(manager.stats(server.uuid))
            for server in _server_store().all()
        ])

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or not valid_server_uuid(str(payload.get("uuid", ""))):
        return jsonify({
            "error": "The data provided in the request could not be validated."
        }), 422

    server_uuid = str(payload["uuid"])
    start_on_completion = bool(payload.get("start_on_completion", False))

    # Fetch full server configuration from Panel if not provided in payload, matching Go Wings installer.New()
    configuration = payload.get("configuration")
    if not isinstance(configuration, dict) or not configuration.get("container"):
        if current_app.config["PANEL_LOCATION"]:
            try:
                configuration = current_app.extensions["remote_client"].get_server_configuration(server_uuid)
            except Exception as err:
                logger.warning("Could not fetch full server configuration from Panel for %s: %s", server_uuid, err)
                if not isinstance(configuration, dict):
                    configuration = dict(payload)
        else:
            if not isinstance(configuration, dict):
                configuration = dict(payload)

    configuration["uuid"] = server_uuid
    _server_store().add(ServerRecord(uuid=server_uuid, configuration=configuration, state="installing"))

    # Begin installation process in the background, exactly matching Wings
    current_app.extensions["process_manager"].install(
        server_uuid,
        configuration,
        reinstall=False,
        start_on_completion=start_on_completion,
    )
    return "", 202


@api.route("/api/servers/<server_uuid>", methods=["GET", "OPTIONS"], provide_automatic_options=False)
@require_authorization
def server_detail(server_uuid: str):
    if request.method == "OPTIONS":
        return "", 204
    server = _server_store().get(server_uuid)
    if server is None:
        return jsonify({"error": "The requested resource does not exist on this instance."}), 404
    return jsonify(server.to_api_response(current_app.extensions["process_manager"].stats(server_uuid)))


@api.route("/api/servers/<server_uuid>/resources", methods=["GET", "OPTIONS"], provide_automatic_options=False)
@require_authorization
def server_resources(server_uuid: str):
    if request.method == "OPTIONS":
        return "", 204
    _, error = _require_server(server_uuid)
    if error:
        return error
    return jsonify(current_app.extensions["process_manager"].stats(server_uuid))


@api.route("/api/servers/<server_uuid>/backup", methods=["GET", "POST", "OPTIONS"], provide_automatic_options=False)
@api.route("/api/servers/<server_uuid>/backups", methods=["GET", "POST", "OPTIONS"], provide_automatic_options=False)
@require_authorization
def server_backups(server_uuid: str):
    if request.method == "OPTIONS":
        return "", 204
    _, error = _require_server(server_uuid)
    if error:
        return error
    filesystem = _filesystem(server_uuid)
    if request.method == "GET":
        return jsonify(filesystem.list_backups())

    payload = request.get_json(silent=True) or {}
    backup_uuid = str(payload.get("uuid") or uuid.uuid4())
    name = payload.get("name")
    ignore = payload.get("ignore")

    app = current_app._get_current_object()

    def _async_backup():
        with app.app_context():
            try:
                backup = filesystem.create_backup(backup_uuid, name, ignore)
                bus.publish(server_uuid, "backup completed", json.dumps({
                    "uuid": backup_uuid,
                    "is_successful": True,
                    "checksum": backup.get("checksum", ""),
                    "checksum_type": "sha256",
                    "file_size": backup.get("bytes", 0),
                }))
                if app.config["PANEL_LOCATION"]:
                    remote = app.extensions["remote_client"]
                    remote.set_backup_status(backup_uuid, {
                        "checksum": backup.get("checksum", ""),
                        "checksum_type": "sha256",
                        "size": backup.get("bytes", 0),
                        "successful": True,
                        "parts": [],
                    })
                logger.info("Backup %s for %s completed successfully", backup_uuid, server_uuid)
            except Exception as exc:
                logger.error("Failed creating backup %s for %s: %s", backup_uuid, server_uuid, exc)
                bus.publish(server_uuid, "backup completed", json.dumps({
                    "uuid": backup_uuid,
                    "is_successful": False,
                    "checksum": "",
                    "checksum_type": "sha256",
                    "file_size": 0,
                }))
                if app.config["PANEL_LOCATION"]:
                    try:
                        remote = app.extensions["remote_client"]
                        remote.set_backup_status(backup_uuid, {
                            "checksum": "",
                            "checksum_type": "sha256",
                            "size": 0,
                            "successful": False,
                            "parts": [],
                        })
                    except Exception:
                        pass

    Thread(target=_async_backup, daemon=True).start()
    return jsonify({"uuid": backup_uuid, "successful": True}), 202


@api.route("/api/servers/<server_uuid>/backup/<backup_uuid>", methods=["DELETE", "OPTIONS"], provide_automatic_options=False)
@api.route("/api/servers/<server_uuid>/backups/<backup_uuid>", methods=["DELETE", "OPTIONS"], provide_automatic_options=False)
@require_authorization
def delete_backup(server_uuid: str, backup_uuid: str):
    if request.method == "OPTIONS":
        return "", 204
    _, error = _require_server(server_uuid)
    if error:
        return error
    try:
        _filesystem(server_uuid).delete_backup(backup_uuid)
        return "", 204
    except FilesystemError as exc:
        return jsonify({"error": str(exc)}), 404


@api.route("/api/servers/<server_uuid>/backup/<backup_uuid>/download", methods=["GET", "OPTIONS"], provide_automatic_options=False)
@api.route("/api/servers/<server_uuid>/backups/<backup_uuid>/download", methods=["GET", "OPTIONS"], provide_automatic_options=False)
@require_authorization
def download_backup(server_uuid: str, backup_uuid: str):
    if request.method == "OPTIONS":
        return "", 204
    _, error = _require_server(server_uuid)
    if error:
        return error
    try:
        archive = _filesystem(server_uuid).backup_path(backup_uuid)
        return send_file(archive, as_attachment=True, download_name=archive.name, mimetype="application/gzip")
    except FilesystemError as exc:
        return jsonify({"error": str(exc)}), 404


@api.route("/api/servers/<server_uuid>/backup/<backup_uuid>/restore", methods=["POST", "OPTIONS"], provide_automatic_options=False)
@api.route("/api/servers/<server_uuid>/backups/<backup_uuid>/restore", methods=["POST", "OPTIONS"], provide_automatic_options=False)
@require_authorization
def restore_backup(server_uuid: str, backup_uuid: str):
    if request.method == "OPTIONS":
        return "", 204
    server, error = _require_server(server_uuid)
    if error:
        return error
    payload = request.get_json(silent=True) or {}
    truncate_directory = bool(payload.get("truncate_directory", False))
    manager = current_app.extensions["process_manager"]
    app = current_app._get_current_object()

    def _async_restore():
        with app.app_context():
            try:
                manager.stop(server_uuid, server.configuration)
                _filesystem(server_uuid).restore_backup(backup_uuid, truncate_directory=truncate_directory)
                bus.publish(server_uuid, "backup restore completed")
                if app.config["PANEL_LOCATION"]:
                    remote = app.extensions["remote_client"]
                    remote.send_restoration_status(backup_uuid, True)
                logger.info("Backup %s for %s restored successfully", backup_uuid, server_uuid)
            except Exception as exc:
                logger.error("Failed restoring backup %s for %s: %s", backup_uuid, server_uuid, exc)
                if app.config["PANEL_LOCATION"]:
                    try:
                        remote = app.extensions["remote_client"]
                        remote.send_restoration_status(backup_uuid, False)
                    except Exception:
                        pass

    Thread(target=_async_restore, daemon=True).start()
    return "", 202


@api.route(
    "/api/servers/<server_uuid>/files/list-directory",
    methods=["GET", "OPTIONS"],
    provide_automatic_options=False,
)
@require_authorization
def list_server_directory(server_uuid: str):
    if request.method == "OPTIONS":
        return "", 204
    if _server_store().get(server_uuid) is None:
        return jsonify({"error": "The requested resource does not exist on this instance."}), 404
    try:
        return jsonify(_filesystem(server_uuid).list_directory(request.args.get("directory", "/")))
    except FilesystemError as error:
        return jsonify({"error": str(error)}), 404


def _require_server(server_uuid: str):
    server = _server_store().get(server_uuid)
    if server is None:
        return None, (jsonify({"error": "The requested resource does not exist on this instance."}), 404)
    return server, None


@api.route("/api/servers/<server_uuid>/files/contents", methods=["GET", "OPTIONS"], provide_automatic_options=False)
@require_authorization
def file_contents(server_uuid: str):
    if request.method == "OPTIONS":
        return "", 204
    server, error = _require_server(server_uuid)
    if error:
        return error
    try:
        target, stat = _filesystem(server_uuid).read(request.args.get("file", ""))
        response = current_app.make_response(target.read_bytes())
        response.headers["X-Mime-Type"] = stat["mime"]
        response.headers["Content-Length"] = str(stat["size"])
        if request.args.get("download") is not None:
            response.headers["Content-Disposition"] = f'attachment; filename="{target.name}"'
            response.headers["Content-Type"] = "application/octet-stream"
        else:
            response.headers["Content-Type"] = "text/plain; charset=utf-8"
        return response
    except FilesystemError as exc:
        return jsonify({"error": str(exc)}), 404


@api.route("/api/servers/<server_uuid>/files/write", methods=["POST", "OPTIONS"], provide_automatic_options=False)
@require_authorization
def write_file(server_uuid: str):
    if request.method == "OPTIONS":
        return "", 204
    _, error = _require_server(server_uuid)
    if error:
        return error
    file_param = request.args.get("file") or ""
    if not file_param:
        return jsonify({"error": "A file parameter is required."}), 400
    try:
        content = request.get_data()
        _filesystem(server_uuid).write(file_param, content)
        return "", 204
    except (FilesystemError, PermissionError, OSError) as exc:
        logger.warning("Failed writing to file %s for server %s: %s", file_param, server_uuid, exc)
        return jsonify({"error": str(exc)}), 400


@api.route("/api/servers/<server_uuid>/files/upload", methods=["POST", "OPTIONS"], provide_automatic_options=False)
@require_authorization
def upload_files(server_uuid: str):
    if request.method == "OPTIONS":
        return "", 204
    _, error = _require_server(server_uuid)
    if error:
        return error
    directory = request.args.get("directory", request.args.get("root", "/"))
    try:
        result = _filesystem(server_uuid).upload(directory, request.files.values())
        return jsonify(result), 201
    except FilesystemError as exc:
        return jsonify({"error": str(exc)}), 400


@api.route("/api/servers/<server_uuid>/files/rename", methods=["PUT", "OPTIONS"], provide_automatic_options=False)
@require_authorization
def rename_files(server_uuid: str):
    if request.method == "OPTIONS":
        return "", 204
    _, error = _require_server(server_uuid)
    if error:
        return error
    payload = request.get_json(silent=True) or {}
    try:
        _filesystem(server_uuid).rename(payload.get("root", "/"), payload.get("files", []))
        return "", 204
    except FilesystemError as exc:
        return jsonify({"error": str(exc)}), 400


@api.route("/api/servers/<server_uuid>/files/copy", methods=["POST", "OPTIONS"], provide_automatic_options=False)
@require_authorization
def copy_file(server_uuid: str):
    if request.method == "OPTIONS":
        return "", 204
    _, error = _require_server(server_uuid)
    if error:
        return error
    try:
        _filesystem(server_uuid).copy((request.get_json(silent=True) or {}).get("location", ""))
        return "", 204
    except FilesystemError as exc:
        return jsonify({"error": str(exc)}), 400


@api.route("/api/servers/<server_uuid>/files/create-directory", methods=["POST", "OPTIONS"], provide_automatic_options=False)
@require_authorization
def create_directory(server_uuid: str):
    if request.method == "OPTIONS":
        return "", 204
    _, error = _require_server(server_uuid)
    if error:
        return error
    payload = request.get_json(silent=True) or {}
    try:
        _filesystem(server_uuid).create_directory(payload.get("name", ""), payload.get("path", "/"))
        return "", 204
    except FilesystemError as exc:
        return jsonify({"error": str(exc)}), 400


@api.route("/api/servers/<server_uuid>/files/delete", methods=["POST", "OPTIONS"], provide_automatic_options=False)
@require_authorization
def delete_files(server_uuid: str):
    if request.method == "OPTIONS":
        return "", 204
    _, error = _require_server(server_uuid)
    if error:
        return error
    payload = request.get_json(silent=True) or {}
    try:
        _filesystem(server_uuid).delete(payload.get("root", "/"), payload.get("files", []))
        return "", 204
    except FilesystemError as exc:
        return jsonify({"error": str(exc)}), 400


@api.route("/api/servers/<server_uuid>/files/chmod", methods=["POST", "OPTIONS"], provide_automatic_options=False)
@require_authorization
def chmod_files(server_uuid: str):
    if request.method == "OPTIONS":
        return "", 204
    _, error = _require_server(server_uuid)
    if error:
        return error
    payload = request.get_json(silent=True) or {}
    try:
        _filesystem(server_uuid).chmod(payload.get("root", "/"), payload.get("files", []))
        return "", 204
    except FilesystemError as exc:
        return jsonify({"error": str(exc)}), 400


@api.route("/api/servers/<server_uuid>/files/compress", methods=["POST", "OPTIONS"], provide_automatic_options=False)
@require_authorization
def compress_files(server_uuid: str):
    if request.method == "OPTIONS":
        return "", 204
    _, error = _require_server(server_uuid)
    if error:
        return error
    payload = request.get_json(silent=True) or {}
    try:
        return jsonify(_filesystem(server_uuid).compress(
            payload.get("root", "/"),
            payload.get("files", []),
            payload.get("file") or payload.get("name"),
            payload.get("type", payload.get("compression", "tar.gz")),
        ))
    except FilesystemError as exc:
        return jsonify({"error": str(exc)}), 400


@api.route("/api/servers/<server_uuid>/files/decompress", methods=["POST", "OPTIONS"], provide_automatic_options=False)
@require_authorization
def decompress_files(server_uuid: str):
    if request.method == "OPTIONS":
        return "", 204
    _, error = _require_server(server_uuid)
    if error:
        return error
    payload = request.get_json(silent=True) or {}
    try:
        _filesystem(server_uuid).decompress(payload.get("root", "/"), payload.get("file", ""))
        return "", 204
    except FilesystemError as exc:
        return jsonify({"error": str(exc)}), 400


@api.route(
    "/api/servers/<server_uuid>/power",
    methods=["POST", "OPTIONS"],
    provide_automatic_options=False,
)
@require_authorization
def server_power(server_uuid: str):
    if request.method == "OPTIONS":
        return "", 204
    server = _server_store().get(server_uuid)
    if server is None:
        return jsonify({"error": "The requested resource does not exist on this instance."}), 404
    payload = request.get_json(silent=True) or {}
    action = payload.get("action")
    try:
        wait_seconds = int(payload.get("wait_seconds", 30))
    except (TypeError, ValueError):
        wait_seconds = 30
    wait_seconds = max(0, min(wait_seconds, 300))
    if action not in {"start", "stop", "restart", "kill"}:
        return jsonify({
            "error": 'The power action provided was not valid, should be one of "stop", "start", "restart", "kill"'
        }), 422
    if action in {"start", "restart"} and server.is_suspended:
        return jsonify({"error": "Cannot start or restart a server that is suspended."}), 400

    manager = current_app.extensions["process_manager"]
    app = current_app._get_current_object()

    def handle_power():
        with app.app_context():
            srv = _server_store().get(server_uuid)
            if not srv:
                return
            if action in {"start", "restart"} and app.config["PANEL_LOCATION"]:
                try:
                    cfg = app.extensions["remote_client"].get_server_configuration(server_uuid)
                    if isinstance(cfg, dict) and cfg:
                        srv = _server_store().update_configuration(server_uuid, cfg)
                except Exception as err:
                    logger.warning("Could not refresh server config on power %s for %s: %s", action, server_uuid, err)
            try:
                if action == "start":
                    manager.start(server_uuid, srv.configuration)
                elif action == "restart":
                    manager.restart(server_uuid, srv.configuration, wait_seconds)
                elif action == "kill":
                    manager.kill(server_uuid)
                else:
                    manager.stop(server_uuid, srv.configuration, wait_seconds)
            except Exception as err:
                logger.warning("Error processing power action %s for %s: %s", action, server_uuid, err)

    Thread(target=handle_power, daemon=True).start()
    return "", 202


@api.route("/api/servers/<server_uuid>/install", methods=["POST", "OPTIONS"], provide_automatic_options=False)
@require_authorization
def install_server(server_uuid: str):
    if request.method == "OPTIONS":
        return "", 204
    server, error = _require_server(server_uuid)
    if error:
        return error
    if server.state == "installing":
        return jsonify({"error": "The server is already being installed."}), 409
    if current_app.config["PANEL_LOCATION"]:
        try:
            configuration = current_app.extensions["remote_client"].get_server_configuration(server_uuid)
            server = _server_store().update_configuration(server_uuid, configuration)
        except PanelRemoteError as exc:
            return jsonify({"error": str(exc)}), 502
    try:
        current_app.extensions["process_manager"].install(server_uuid, server.configuration, reinstall=False)
    except RuntimeUnavailableError as exc:
        return jsonify({"error": str(exc)}), 503
    except (RuntimeCommandError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    return "", 202


@api.route("/api/servers/<server_uuid>/reinstall", methods=["POST", "OPTIONS"], provide_automatic_options=False)
@require_authorization
def reinstall_server(server_uuid: str):
    if request.method == "OPTIONS":
        return "", 204
    server, error = _require_server(server_uuid)
    if error:
        return error
    if server.state in {"installing", "reinstalling"}:
        return jsonify({"error": "The server is already being installed."}), 409
    if current_app.config["PANEL_LOCATION"]:
        try:
            configuration = current_app.extensions["remote_client"].get_server_configuration(server_uuid)
            server = _server_store().update_configuration(server_uuid, configuration)
        except PanelRemoteError as exc:
            return jsonify({"error": str(exc)}), 502
    try:
        current_app.extensions["process_manager"].reinstall(server_uuid, server.configuration)
    except RuntimeUnavailableError as exc:
        return jsonify({"error": str(exc)}), 503
    except (RuntimeCommandError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    return "", 202


@api.route("/api/servers/<server_uuid>/transfer", methods=["GET", "POST", "DELETE", "OPTIONS"], provide_automatic_options=False)
@require_authorization
def server_transfer(server_uuid: str):
    if request.method == "OPTIONS":
        return "", 204
    server, error = _require_server(server_uuid)
    if error:
        return error
    filesystem = _filesystem(server_uuid)
    if request.method == "GET":
        with _transfers_lock:
            transfer = _transfers.get(server_uuid)
        if transfer is None:
            return jsonify({"status": "none"}), 404
        return jsonify(transfer)
    if request.method == "DELETE":
        with _transfers_lock:
            transfer = _transfers.pop(server_uuid, None)
        if transfer:
            try:
                filesystem.delete_backup(transfer["uuid"])
            except FilesystemError:
                pass
        return "", 204
    transfer_id = str(uuid.uuid4())
    with _transfers_lock:
        previous = _transfers.get(server_uuid)
    if previous:
        try:
            filesystem.delete_backup(previous["uuid"])
        except FilesystemError:
            pass
    try:
        current_app.extensions["process_manager"].stop(server_uuid, server.configuration)
        archive = filesystem.create_backup(transfer_id, f"transfer-{transfer_id}")
    except FilesystemError as exc:
        return jsonify({"error": str(exc)}), 400
    with _transfers_lock:
        _transfers[server_uuid] = {"uuid": transfer_id, "status": "ready", "archive": archive["name"]}
    payload = request.get_json(silent=True) or {}
    destination_url = str(payload.get("destination_url", ""))
    transfer_token = str(payload.get("transfer_token", ""))
    if destination_url:
        parsed = urlparse(destination_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or not transfer_token:
            return jsonify({"error": "destination_url and transfer_token must be valid."}), 400
        with _transfers_lock:
            _transfers[server_uuid]["status"] = "sending"
        Thread(
            target=_send_transfer_worker,
            args=(server_uuid, transfer_id, destination_url, transfer_token, server_uuid),
            daemon=True,
        ).start()
    return jsonify(_transfers[server_uuid]), 202


def _send_transfer_worker(server_uuid: str, transfer_id: str, destination_url: str, token: str, source_uuid: str) -> None:
    archive = _filesystem(server_uuid).backup_path(transfer_id)
    parsed = urlparse(destination_url)
    connection_class = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    connection = connection_class(parsed.netloc, timeout=60)
    boundary = f"----pywings-{uuid.uuid4().hex}"
    prefix = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"server_uuid\"\r\n\r\n"
        f"{source_uuid}\r\n--{boundary}\r\nContent-Disposition: form-data; name=\"archive\"; filename=\"{archive.name}\"\r\n"
        "Content-Type: application/gzip\r\n\r\n"
    ).encode()
    suffix = f"\r\n--{boundary}--\r\n".encode()
    path = parsed.path or "/api/transfers"
    if parsed.query:
        path += f"?{parsed.query}"
    try:
        connection.putrequest("POST", path)
        connection.putheader("Authorization", f"Bearer {token}")
        connection.putheader("Content-Type", f"multipart/form-data; boundary={boundary}")
        connection.putheader("Content-Length", str(len(prefix) + archive.stat().st_size + len(suffix)))
        connection.endheaders()
        connection.send(prefix)
        with archive.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                connection.send(chunk)
        connection.send(suffix)
        response = connection.getresponse()
        if response.status < 200 or response.status >= 300:
            raise RuntimeError(f"destination returned HTTP {response.status}")
        status = "completed"
    except Exception as error:
        status = "failed"
        with _transfers_lock:
            _transfers[server_uuid]["error"] = str(error)
    finally:
        connection.close()
    with _transfers_lock:
        if _transfers.get(server_uuid, {}).get("uuid") == transfer_id:
            _transfers[server_uuid]["status"] = status


@api.route("/api/servers/<server_uuid>/ws/deny", methods=["POST", "OPTIONS"], provide_automatic_options=False)
@require_authorization
def deny_websocket_tokens(server_uuid: str):
    if request.method == "OPTIONS":
        return "", 204
    _, error = _require_server(server_uuid)
    if error:
        return error
    payload = request.get_json(silent=True) or {}
    jtis = payload.get("jtis")
    if not isinstance(jtis, list):
        return jsonify({"error": "The jtis field must be an array."}), 400
    with _denied_websocket_jtis_lock:
        _denied_websocket_jtis.update(str(jti) for jti in jtis if jti)
    return "", 204


@api.route("/api/servers/<server_uuid>/commands", methods=["POST", "OPTIONS"], provide_automatic_options=False)
@require_authorization
def server_commands(server_uuid: str):
    if request.method == "OPTIONS":
        return "", 204
    _, error = _require_server(server_uuid)
    if error:
        return error
    payload = request.get_json(silent=True) or {}
    commands = payload.get("commands")
    if not isinstance(commands, list):
        return jsonify({"error": "The commands field must be an array."}), 400
    manager = current_app.extensions["process_manager"]
    try:
        for command in commands:
            manager.send_command(server_uuid, str(command))
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 502
    return "", 204


@api.route("/api/servers/<server_uuid>/logs", methods=["GET", "OPTIONS"], provide_automatic_options=False)
@require_authorization
def server_logs(server_uuid: str):
    if request.method == "OPTIONS":
        return "", 204
    _, error = _require_server(server_uuid)
    if error:
        return error
    try:
        size = int(request.args.get("size", "100"))
    except ValueError:
        size = 100
    return jsonify({"data": current_app.extensions["process_manager"].read_logs(server_uuid, size)})


@api.route("/api/servers/<server_uuid>", methods=["DELETE"], provide_automatic_options=False)
@require_authorization
def delete_server(server_uuid: str):
    """Remove the runtime container and registry entry, preserving data."""
    if _server_store().get(server_uuid) is None:
        return jsonify({"error": "The requested resource does not exist on this instance."}), 404
    try:
        current_app.extensions["process_manager"].remove(server_uuid)
    except RuntimeUnavailableError as error:
        return jsonify({"error": str(error)}), 503
    except RuntimeCommandError as error:
        return jsonify({"error": str(error)}), 400
    _server_store().remove(server_uuid)
    return "", 204


@api.route("/api/servers/<server_uuid>/sync", methods=["POST", "OPTIONS"], provide_automatic_options=False)
@require_authorization
def sync_server(server_uuid: str):
    """Synchronize a server entry before the remote Panel client exists."""
    if request.method == "OPTIONS":
        return "", 204
    if not valid_server_uuid(server_uuid):
        return jsonify({"error": "The requested resource does not exist on this instance."}), 404
    try:
        configuration = current_app.extensions["remote_client"].get_server_configuration(server_uuid)
        _server_store().update_configuration(server_uuid, configuration)
        # Pre-pull new image and update container metadata immediately if server is not running
        manager = current_app.extensions["process_manager"]
        image = (configuration.get("container") or {}).get("image") or configuration.get("image")
        if image and not manager.is_running(server_uuid):
            try:
                manager.runtime.pull(image)
                manager.runtime.create(server_uuid, image)
            except Exception as err:
                logger.warning("Could not pre-sync runtime container for %s: %s", server_uuid, err)
    except PanelRemoteError as error:
        # Keep a local placeholder if the Panel is temporarily unavailable;
        # Wings can retry sync later without losing server identity.
        _server_store().ensure(server_uuid)
        if current_app.config["PANEL_LOCATION"]:
            return jsonify({"error": str(error)}), 502
    return "", 204


def register_websocket(sock) -> None:
    """Register the Wings websocket handler on the Flask application."""
    @sock.route("/api/servers/<server_uuid>/ws")
    def server_websocket(ws, server_uuid: str):
        if _server_store().get(server_uuid) is None:
            logger.warning("WebSocket rejected connection for non-existent server: %s", server_uuid)
            ws.close()
            return

        logger.info("WebSocket client connected for server %s", server_uuid)
        authenticated = False
        claims: dict = {}
        active = True
        ws_lock = Lock()

        def safe_send(event: str, args=None) -> None:
            message = {"event": event}
            if args is not None:
                message["args"] = args
            with ws_lock:
                try:
                    ws.send(json.dumps(message))
                except Exception:
                    pass

        app = current_app._get_current_object()

        def send_stats() -> None:
            try:
                manager = app.extensions.get("process_manager")
                if manager:
                    stats_data = manager.stats(server_uuid)
                    stats_json = json.dumps(stats_data, separators=(",", ":"))
                    safe_send("stats", [stats_json])
            except Exception:
                pass

        event_queue = bus.subscribe(server_uuid)

        def event_pump():
            while active:
                try:
                    msg = event_queue.get(timeout=0.5)
                except Empty:
                    continue
                except Exception:
                    break

                evt = msg.get("event")
                # Filter events based on claims permissions
                if not authenticated and evt != "jwt error":
                    continue

                perms = claims.get("permissions", [])
                if evt == "transfer logs" and "admin.websocket.transfer" not in perms and "*" not in perms:
                    continue

                safe_send(evt, msg.get("args", []))

        def stats_streamer():
            while active:
                if authenticated:
                    send_stats()
                time.sleep(1.0)

        pump_thread = Thread(target=event_pump, daemon=True)
        pump_thread.start()
        stats_thread = Thread(target=stats_streamer, daemon=True)
        stats_thread.start()

        try:
            while True:
                message = ws.receive()
                if message is None:
                    break
                try:
                    payload = json.loads(message)
                except (TypeError, ValueError):
                    safe_send("jwt error", ["websocket: invalid message"])
                    continue

                event = payload.get("event")
                if event != "auth" and not authenticated:
                    safe_send("jwt error", ["jwt: no jwt present"])
                    continue

                if event != "auth":
                    if event == "send stats":
                        send_stats()
                    elif event == "send logs":
                        for line in current_app.extensions["process_manager"].read_logs(server_uuid):
                            safe_send("console output", [line])
                    elif event == "send command":
                        perms = claims.get("permissions", [])
                        if "control.console" not in perms and "*" not in perms:
                            continue
                        args = payload.get("args") or []
                        command = str(args[0]) if args else ""
                        try:
                            current_app.extensions["process_manager"].send_command(server_uuid, command)
                        except RuntimeError as error:
                            safe_send("console output", [f"[Wings] {error}"])
                    elif event == "set state":
                        args = payload.get("args") or []
                        action = str(args[0]) if args else ""
                        logger.info("WebSocket power action '%s' requested for server %s", action, server_uuid)
                        perms = claims.get("permissions", [])
                        if action == "start" and "control.start" not in perms and "*" not in perms:
                            logger.warning("WebSocket user lacks 'control.start' permission for server %s", server_uuid)
                            continue
                        if action in {"stop", "kill"} and "control.stop" not in perms and "*" not in perms:
                            logger.warning("WebSocket user lacks 'control.stop' permission for server %s", server_uuid)
                            continue
                        if action == "restart" and "control.restart" not in perms and "*" not in perms:
                            logger.warning("WebSocket user lacks 'control.restart' permission for server %s", server_uuid)
                            continue

                        def handle_ws_power(act: str):
                            with app.app_context():
                                srv = _server_store().get(server_uuid)
                                if srv is None:
                                    safe_send("console output", ["[Wings] Server no longer exists."])
                                    return
                                manager = app.extensions["process_manager"]
                                try:
                                    if act in {"start", "restart"} and app.config["PANEL_LOCATION"]:
                                        try:
                                            cfg = app.extensions["remote_client"].get_server_configuration(server_uuid)
                                            if isinstance(cfg, dict) and cfg:
                                                srv = _server_store().update_configuration(server_uuid, cfg)
                                        except Exception as sync_err:
                                            logger.warning("Could not sync server configuration from Panel (%s), using local config", sync_err)
                                    if act == "start":
                                        manager.start(server_uuid, srv.configuration)
                                    elif act == "stop":
                                        manager.stop(server_uuid, srv.configuration)
                                    elif act == "restart":
                                        manager.restart(server_uuid, srv.configuration)
                                    elif act == "kill":
                                        manager.kill(server_uuid)
                                    else:
                                        safe_send("console output", [f"[Wings] Unknown power action: {act}"])
                                except Exception as err:
                                    logger.warning("Error processing power action %s from ws: %s", act, err)
                                    safe_send("console output", [f"[Wings] {err}"])

                        Thread(target=handle_ws_power, args=(action,), daemon=True).start()
                    continue

                # Authentication event
                token = "".join(str(value) for value in payload.get("args", []))
                try:
                    claims = jwt.decode(
                        token,
                        current_app.config["TOKEN"],
                        algorithms=["HS256"],
                        options={
                            "verify_aud": False,
                            "verify_iat": False,
                            "verify_nbf": False,
                        },
                    )
                    claim_server = claims.get("server_uuid", "")
                    if claim_server and claim_server != server_uuid:
                        raise ValueError("jwt: server uuid mismatch")
                    claim_scope = claims.get("scope") or claims.get("scopes")
                    if claim_scope is not None and claim_scope not in ("", "websocket", "*"):
                        if isinstance(claim_scope, list):
                            if "websocket" not in claim_scope and "*" not in claim_scope:
                                raise ValueError("jwt: invalid scope")
                        else:
                            raise ValueError("jwt: invalid scope")
                    with _denied_websocket_jtis_lock:
                        if claims.get("jti") in _denied_websocket_jtis:
                            raise ValueError("jwt: token has been denied")
                    permissions = claims.get("permissions", [])
                    if "websocket.connect" not in permissions and "*" not in permissions:
                        raise ValueError("jwt: missing connect permission")
                except Exception as error:
                    logger.warning("WebSocket authentication failed for server %s: %s", server_uuid, error)
                    safe_send("jwt error", [str(error)])
                    continue

                authenticated = True
                user_id = claims.get("user_uuid", "authenticated")
                logger.info("WebSocket user %s successfully authenticated for server %s", user_id, server_uuid)
                safe_send("auth success")
                current = _server_store().get(server_uuid)
                safe_send("status", [current.state if current else "offline"])
                send_stats()
        finally:
            active = False
            bus.unsubscribe(server_uuid, event_queue)
            logger.info("WebSocket disconnected for server %s", server_uuid)
            try:
                ws.close()
            except Exception:
                pass
