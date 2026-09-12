"""Production WSGI entrypoint for gunicorn on Linux."""

import os

os.environ["PROOT_NO_SECCOMP"] = "1"
os.environ["GLIBC_TUNABLES"] = "glibc.pthread.rseq=0"

# Check GitHub version.txt at launch and auto-upgrade if newer version exists
try:
    from wings.updater import check_and_update_on_launch
    check_and_update_on_launch()
except Exception:
    pass

from wings import create_app


app = create_app()
