"""WSGI entry point.

    gunicorn -c deploy/gunicorn.conf.py deploy.wsgi:application

Nginx serves index.html itself, so this process only answers /ocr and /health.
"""

from server import app as application

# gunicorn looks for `application`; keep `app` too for tools that expect it.
app = application
