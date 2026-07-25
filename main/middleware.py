"""Middleware that has to run before everything else."""

import logging

from django.db import connection
from django.http import HttpResponse

logger = logging.getLogger(__name__)

HEALTH_PATH = "/healthz"


class HealthCheckMiddleware:
    """Answer the Kubernetes probes, ahead of the rest of the stack.

    This is **first** in MIDDLEWARE, and it has to stay there. Returning
    before `get_response` means nothing below it runs, which is the whole
    point -- three separate things downstream would otherwise break the
    probe:

    * ``SecurityMiddleware`` would answer with a 301 to https, because the
      kubelet dials the pod over plain HTTP and sends no X-Forwarded-Proto.
      Kubernetes counts a 3xx as success, so the probe would pass on a
      completely broken app.
    * ``ALLOWED_HOSTS`` would reject the request with a 400: with no Host
      override on the probe the kubelet sends the pod IP, which is not a name
      this app can know in advance.
    * ``cookie_consent``'s CleanCookiesMiddleware queries the database in
      ``process_response``. On an unreachable database that raises *after*
      the health view has already returned, so the probe would spend a second
      connection timeout and report a 500 traceback instead of a clean 503.

    The database query below is deliberate: a probe that cannot fail is not a
    probe. `/` could not fail -- it never got past the redirect.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path == HEALTH_PATH:
            return self.health_response()
        return self.get_response(request)

    @staticmethod
    def health_response() -> HttpResponse:
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
        except Exception:
            logger.exception("Health check failed: database is unreachable.")
            return HttpResponse("db unavailable\n", status=503,
                                content_type="text/plain")
        return HttpResponse("ok\n", content_type="text/plain")
