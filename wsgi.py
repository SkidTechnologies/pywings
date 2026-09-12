"""Production WSGI entrypoint for gunicorn on Linux."""

from wings import create_app


app = create_app()
