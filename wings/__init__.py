"""Python implementation of the Pterodactyl Wings control plane."""

from flask import Flask, request
from flask_sock import Sock
from pathlib import Path
import logging
import os
import time
import uuid

from wings.config import Settings
from wings.logger import setup_logging
from wings.servers import ServerStore
from wings.processes import ProcessManager
from wings.runtime import ProotRuntime
from wings.remote import PanelRemoteClient


logger = logging.getLogger("wings")
http_logger = logging.getLogger("wings.http")


def create_app(settings: Settings | None = None) -> Flask:
    """Create and configure the Wings Flask application.

    Keeping application creation in a factory makes the service easy to test and
    leaves room for multiple configurations in later stages.
    """
    app = Flask(__name__)
    app.config.from_mapping((settings or Settings.from_file()).as_flask_config())

    # Initialize extended console logging matching Wings format
    setup_logging(bool(app.config.get("DEBUG", False)))

    app.config["MAX_CONTENT_LENGTH"] = int(app.config["UPLOAD_LIMIT"]) * 1024 * 1024
    app.extensions["server_store"] = ServerStore(app.config["DATA_DIRECTORY"])
    remote_client = PanelRemoteClient(
        app.config["PANEL_LOCATION"], app.config["TOKEN_ID"], app.config["TOKEN"]
    )
    app.extensions["remote_client"] = remote_client
    runtime_data_dir = Path(app.config["DATA_DIRECTORY"]).resolve() / "runtime"
    proot_custom_path = app.config.get("PROOT_PATH") or None
    runtime = ProotRuntime(data_directory=runtime_data_dir, proot_path=proot_custom_path)
    app.extensions["container_runtime"] = runtime

    # Initialize activity event manager
    activity_manager = None
    try:
        from wings.activity import ActivityManager
        activity_manager = ActivityManager(remote_client=remote_client)
        activity_manager.start()
        app.extensions["activity_manager"] = activity_manager
    except Exception as err:
        logger.warning("Could not start activity manager: %s", err)

    app.extensions["process_manager"] = ProcessManager(
        app.extensions["server_store"],
        runtime,
        app.config["ALLOWED_MOUNTS"],
        remote_client=remote_client,
        activity_manager=activity_manager,
    )
    sock = Sock(app)

    # Reset server states and clean up deleted servers & unused egg images on boot
    def _startup_sync_and_cleanup():
        if not app.config["PANEL_LOCATION"]:
            return
        remote_client.reset_servers_state()
        time.sleep(3)
        try:
            panel_servers = remote_client.get_servers()
            if not panel_servers:
                return
            panel_uuids = set()
            for item in panel_servers:
                u = item.get("uuid") or (item.get("settings") or {}).get("uuid") or (item.get("attributes") or {}).get("uuid")
                if u:
                    panel_uuids.add(str(u).lower())

            if not panel_uuids:
                return

            pm = app.extensions.get("process_manager")
            store = app.extensions.get("server_store")
            if not pm or not store:
                return

            # Clean up local directories on disk for servers deleted in Panel
            data_dir = Path(app.config["DATA_DIRECTORY"]).resolve()
            if data_dir.is_dir():
                for entry in data_dir.iterdir():
                    if entry.is_dir() and len(entry.name) == 36 and entry.name.count("-") == 4:
                        if entry.name.lower() not in panel_uuids:
                            logger.info("Cleaning up unlisted server directory on disk: %s", entry.name)
                            pm.remove(entry.name, purge_files=True)

            # Clean up server records in store for deleted servers
            for srv in store.all():
                if srv.uuid.lower() not in panel_uuids:
                    logger.info("Cleaning up unlisted server from local store: %s", srv.uuid)
                    pm.remove(srv.uuid, purge_files=True)

            # Clean up unreferenced/unused OCI egg images and rootfs directories
            try:
                active_images = set()
                for srv in store.all():
                    cfg = srv.configuration or {}
                    img = (cfg.get("container") or {}).get("image") or cfg.get("image")
                    if img:
                        active_images.add(str(img).strip())
                pm.runtime.oci_manager.prune_unused_images(active_images)
            except Exception as prune_err:
                logger.debug("Image rootfs prune encountered error: %s", prune_err)

        except Exception as err:
            logger.warning("Startup sync & cleanup failed: %s", err)

    if app.config["PANEL_LOCATION"]:
        import threading
        threading.Thread(target=_startup_sync_and_cleanup, daemon=True).start()

    # Start integrated SFTP server matching Wings port configuration
    try:
        from wings.sftp import SFTPServer
        sftp_server = SFTPServer(
            host=app.config.get("SFTP_BIND_ADDRESS", "0.0.0.0"),
            port=int(app.config.get("SFTP_BIND_PORT", 2022)),
            data_directory=app.config["DATA_DIRECTORY"],
            remote_client=remote_client,
            store=app.extensions["server_store"],
        )
        sftp_server.start()
        app.extensions["sftp_server"] = sftp_server
    except Exception as err:
        logger.warning("Could not start SFTP server: %s", err)

    # Start background auto-updater to keep pywings up to date with remote git repository
    try:
        from wings.updater import AutoUpdater
        updater = AutoUpdater(
            app=app,
            interval_seconds=int(os.getenv("WINGS_UPDATE_INTERVAL", 60)),
            enabled=os.getenv("WINGS_AUTO_UPDATE", "true").lower() in {"1", "true", "yes", "on"},
        )
        updater.start()
        app.extensions["updater"] = updater
    except Exception as err:
        logger.warning("Could not initialize auto-updater: %s", err)

    @app.before_request
    def record_request_start():
        request._wings_start_time = time.monotonic()

    @app.after_request
    def add_cors_headers(response):
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        response.headers["X-Request-ID"] = request_id
        origin = request.headers.get("Origin")
        panel_location = app.config["PANEL_LOCATION"]
        allowed_origin = panel_location
        if origin and (origin == panel_location or panel_location == "*"):
            allowed_origin = origin
        if allowed_origin:
            response.headers["Access-Control-Allow-Origin"] = allowed_origin
        response.headers["Access-Control-Allow-Credentials"] = "true"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PATCH, PUT, DELETE, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = (
            "Accept, Accept-Encoding, Authorization, Cache-Control, Content-Type, "
            "Content-Length, Origin, X-Real-IP, X-CSRF-Token"
        )
        response.headers["Access-Control-Max-Age"] = "7200"
        response.headers["Access-Control-Expose-Headers"] = "X-Request-ID"

        # Log incoming HTTP requests
        start_time = getattr(request, "_wings_start_time", None)
        latency = (time.monotonic() - start_time) * 1000 if start_time else 0.0
        client_ip = request.headers.get("X-Forwarded-For", request.remote_addr)
        http_logger.info(
            "%s %s -> %s (%.2fms) [ip:%s]",
            request.method,
            request.path,
            response.status_code,
            latency,
            client_ip,
        )
        return response

    @app.errorhandler(404)
    def not_found(_error):
        # Gin's default response used by Wings for an unknown route.
        return "404 page not found\n", 404, {"Content-Type": "text/plain; charset=utf-8"}

    @app.errorhandler(413)
    def request_too_large(_error):
        return {"error": "The uploaded file exceeds the configured upload limit."}, 413

    @app.errorhandler(400)
    def bad_request(_error):
        return {"error": "The request could not be understood."}, 400

    @app.errorhandler(500)
    def internal_error(_error):
        return {"error": "An unexpected error was encountered while processing this request."}, 500

    from wings.api import api
    from wings.api import register_websocket

    app.register_blueprint(api)
    register_websocket(sock)
    return app
