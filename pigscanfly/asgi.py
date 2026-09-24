"""
ASGI config for pigscanfly project.

It exposes the ASGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/5.2/howto/deployment/asgi/
"""

import os

from configurations.asgi import get_asgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'pigscanfly.settings')

# Prod unless told otherwise. This module is only ever loaded by an
# application server, and the one that serves the site (start-server.sh)
# exports Prod already; defaulting to Dev meant a server started any other way
# came up with DEBUG on, ALLOWED_HOSTS=['*'] and the insecure development
# SECRET_KEY. Local development runs through manage.py, which keeps Dev.
os.environ.setdefault(
    'DJANGO_CONFIGURATION',
    os.getenv("ENVIRONMENT", 'Prod'))


application = get_asgi_application()
