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
    app.extensions["process_manager"] = ProcessManager(
        app.extensions["server_store"],
        runtime,
        app.config["ALLOWED_MOUNTS"],
        remote_client=remote_client,
    )
    sock = Sock(app)

    # Reset any server states left as 'installing' or 'restoring' on the Panel on daemon boot
    if app.config["PANEL_LOCATION"]:
        import threading
        threading.Thread(target=remote_client.reset_servers_state, daemon=True).start()

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
