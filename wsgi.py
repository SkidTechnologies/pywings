"""Production WSGI entrypoint for gunicorn on Linux."""

import os

os.environ["PROOT_NO_SECCOMP"] = "1"
os.environ["GLIBC_TUNABLES"] = "glibc.pthread.rseq=0"

from wings import create_app


app = create_app()
