"""Development entry point for the Python Wings implementation."""

import os

# Ensure nested PRoot environments disable seccomp ptrace acceleration (avoids exit code 182)
os.environ["PROOT_NO_SECCOMP"] = "1"

from wings import create_app


app = create_app()


if __name__ == "__main__":
    app.run(host=app.config["HOST"], port=app.config["PORT"])
