"""Development entry point for the Python Wings implementation."""

import os

# Ensure nested PRoot environments disable seccomp ptrace acceleration and bypass rseq SIGSEGV
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


if __name__ == "__main__":
    app.run(host=app.config["HOST"], port=app.config["PORT"])
