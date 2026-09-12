"""Development entry point for the Python Wings implementation."""

import os

# Ensure nested PRoot environments disable seccomp ptrace acceleration and bypass rseq SIGSEGV
os.environ["PROOT_NO_SECCOMP"] = "1"
os.environ["GLIBC_TUNABLES"] = "glibc.pthread.rseq=0"

from wings import create_app


app = create_app()


if __name__ == "__main__":
    app.run(host=app.config["HOST"], port=app.config["PORT"])
