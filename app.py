"""Development entry point for the Python Wings implementation."""

from wings import create_app


app = create_app()


if __name__ == "__main__":
    app.run(host=app.config["HOST"], port=app.config["PORT"])
