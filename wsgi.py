"""Production WSGI entrypoint for gunicorn on Linux."""

import os

os.environ["PROOT_NO_SECCOMP"] = "1"
os.environ["UDOCKER_DEFAULT_EXECUTION_MODE"] = "P1"

from wings import create_app


app = create_app()
