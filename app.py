"""Development entry point for the Python Wings implementation."""

import os
import signal
import sys

# Ensure nested PRoot environments disable seccomp ptrace acceleration and bypass rseq SIGSEGV
os.environ["PROOT_NO_SECCOMP"] = "1"
os.environ["GLIBC_TUNABLES"] = "glibc.pthread.rseq=0"

# CLI Configuration Helper
if "--configure" in sys.argv:
    from wings.configure import run_configure
    run_configure(sys.argv[1:])
    sys.exit(0)

# Check GitHub version.txt at launch and auto-upgrade if newer version exists
try:
    from wings.updater import check_and_update_on_launch
    check_and_update_on_launch()
except Exception:
    pass

from wings import create_app


app = create_app()


def _shutdown_handler(signum, frame):
    print("\n[INFO] [daemon] Shutdown signal received. Gracefully stopping pywings...", flush=True)
    if "sftp_server" in app.extensions:
        try:
            app.extensions["sftp_server"].stop()
        except Exception:
            pass
    if "updater" in app.extensions:
        try:
            app.extensions["updater"].stop()
        except Exception:
            pass
    sys.exit(0)


try:
    signal.signal(signal.SIGINT, _shutdown_handler)
    signal.signal(signal.SIGTERM, _shutdown_handler)
except (ValueError, AttributeError):
    pass


if __name__ == "__main__":
    import socket
    import time

    host = str(app.config["HOST"])
    port = int(app.config["PORT"])

    # Wait if port is temporarily in TIME_WAIT from updater relaunch
    for attempt in range(20):
        test_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        test_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            test_sock.bind((host, port))
            test_sock.close()
            break
        except OSError:
            test_sock.close()
            if attempt == 0:
                print(f"[INFO] [server] Port {port} is releasing from updater relaunch; waiting...", flush=True)
            time.sleep(0.5)

    app.run(host=host, port=port)
