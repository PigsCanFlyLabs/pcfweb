#!/usr/bin/env bash
set -euo pipefail
# start-server.sh
cd /opt/app
export DJANGO_CONFIGURATION=${DJANGO_CONFIGURATION:-"Prod"}
export DJANGO_SETTINGS_MODULE=${DJANGO_SETTINGS_MODULE:-"pigscanfly.settings"}
# Run migrations + load the bootstrap fixtures on the primary only
if [ -n "${PRIMARY:-}" ]; then
  ./manage.py migrate
  # The admin account, from DJANGO_SUPERUSER_* in pcfweb-secret: created on a
  # fresh database, converged (rotated password, drifted flags) after that,
  # skipped with a log line when the variables are absent. Right after
  # migrate because it needs the auth tables, and before
  # backfill_email_identities so the backfill claims the admin's
  # EmailIdentity row in this boot rather than the next one.
  #
  # Non-fatal on purpose: the command fails loudly on a half-set pair or a
  # username collision, and an admin nobody can log into is still a lesser
  # incident than a store that stopped selling books over it. The `||` is
  # what keeps `set -e` from turning that exit code into a boot failure.
  ./manage.py ensure_admin_account \
    || echo "start-server.sh: ensure_admin_account failed; starting anyway." >&2
  ./manage.py seed_products
  # Close any old-replica signup window without making startup depend on
  # legacy data being perfectly clean. Row-level residue is already tolerated
  # inside the command and exits 0; this `||` is only for a command-level
  # failure under `set -e` (import error, broken query, missing table, ...),
  # so a busted cleanup still does not stop the primary from booting.
  ./manage.py backfill_email_identities \
    || echo "start-server.sh: backfill_email_identities failed; starting anyway." >&2
  # Now that the catalogue is up to date, check that every product it sells
  # as a download actually has its archive in this image, and log + email the
  # owner about any that do not. Before this, the first thing to notice a
  # missing book file was a customer who had already paid for it.
  #
  # Deliberately not fatal, and belt-and-braces about it: the command exits 0
  # on a missing archive by design (it takes --fail for the callers that do
  # want an exit code), and this `||` additionally covers it dying for some
  # unrelated reason under `set -e`. One book we cannot deliver is not a
  # reason to stop serving the rest of the site -- and this pod is the only
  # one that runs migrations, so failing here would block a deploy on it.
  ./manage.py check_book_assets \
    || echo "start-server.sh: check_book_assets failed; starting anyway." >&2
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
