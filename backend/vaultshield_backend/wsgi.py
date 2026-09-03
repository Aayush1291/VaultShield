"""WSGI entry point for the VaultShield backend.

Exposes the WSGI callable as a module-level ``application``. This is what
the compose stack's `web` service serves through gunicorn.
"""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "vaultshield_backend.settings")

application = get_wsgi_application()
