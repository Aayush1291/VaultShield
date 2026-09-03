"""ASGI entry point for the VaultShield backend.

Exposes the ASGI callable as a module-level ``application``. design.md's
compose topology runs a separate `channels` service against this entry
point, reserved for future live-update support and unused by the MVP
review loop.
"""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "vaultshield_backend.settings")

application = get_asgi_application()
