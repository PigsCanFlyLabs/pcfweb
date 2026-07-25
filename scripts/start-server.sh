#!/usr/bin/env bash
set -euo pipefail
# start-server.sh
cd /opt/app
export DJANGO_CONFIGURATION=${DJANGO_CONFIGURATION:-"Prod"}
export DJANGO_SETTINGS_MODULE=${DJANGO_SETTINGS_MODULE:-"pigscanfly.settings"}
# Run migrations + load the bootstrap fixtures on the primary only
if [ -n "${PRIMARY:-}" ]; then
  ./manage.py migrate
  ./manage.py seed_products
fi

# Keep this under the ingress/proxy read timeout and comfortably above
# STRIPE_TIMEOUT, so a slow Stripe call returns an error the view can handle
# rather than having the worker killed mid-request.
GUNICORN_TIMEOUT=${GUNICORN_TIMEOUT:-60}

# Both processes are supervised: whichever exits first takes the container
# down, so Kubernetes restarts it. Previously gunicorn ran backgrounded behind
# a foreground nginx, so a dead gunicorn left the container "up" and serving
# 502s until a liveness probe happened to notice.
terminate() {
  trap - TERM INT
  kill -TERM "${gunicorn_pid:-}" "${nginx_pid:-}" 2>/dev/null || true
  wait || true
  exit 0
}
trap terminate TERM INT

# No --access-logfile: nginx already logs every request to stdout, and the
# probes hit /healthz every few seconds on every pod (nginx drops those, see
# conf/nginx.default).
gunicorn pigscanfly.wsgi \
  --user www-data \
  --bind 0.0.0.0:8010 \
  --workers 4 \
  --timeout "$GUNICORN_TIMEOUT" \
  --error-logfile - &
gunicorn_pid=$!

nginx -g "daemon off;" &
nginx_pid=$!

# Exit as soon as either one does, propagating its status.
status=0
wait -n "$gunicorn_pid" "$nginx_pid" || status=$?
echo "start-server.sh: a supervised process exited with status ${status}; shutting down." >&2
kill -TERM "$gunicorn_pid" "$nginx_pid" 2>/dev/null || true
wait || true
exit "$status"
