#!/bin/bash
set -ex

# ---------------------------------------------------------------------------
# Refuse to run against anything that looks like production, before touching
# a database or generating anything.
#
# manage.py resolves the configuration with
#
#     os.environ.setdefault('DJANGO_CONFIGURATION', os.getenv("ENVIRONMENT", 'Dev'))
#
# and setdefault does NOT override an already-exported value. That is two
# separate doors into Prod, not one: an exported DJANGO_CONFIGURATION=Prod
# wins outright, and an exported ENVIRONMENT=Prod is read by the fallback and
# selects Prod even with DJANGO_CONFIGURATION unset. Prod.DATABASES then reads
# DBNAME/DBUSER/DBPASSWORD/DBHOST straight from the environment.
#
# So a developer in a production shell -- a sourced .env, a leftover
# `kubectl exec` environment, the terminal they last deployed from -- could
# run a script named run_local.sh and apply migrations plus fixture upserts to
# the live database. seed_products overwrites fixture-owned fields on rows
# that already exist, so that is a data-loss path and not merely an
# inconvenience.
#
# Belt and braces, because the two halves fail differently. This refusal tells
# the developer what is wrong; the forced Dev below means an inherited value
# cannot select Prod even if a new door is added later. Forcing alone would
# silently redirect someone who genuinely believed they were pointing at a
# particular database, and a confusing silent success is worse than an
# explicit refusal naming the variable to unset.
# ---------------------------------------------------------------------------
refuse_production() {
  set +x
  echo >&2
  echo "ERROR: ${1} is set, so this looks like a production shell." >&2
  echo >&2
  echo "run_local.sh runs 'migrate' and 'seed_products'. seed_products" >&2
  echo "overwrites fixture-owned fields on rows that already exist, so" >&2
  echo "running it against a live database loses data. Refusing to touch" >&2
  echo "any database from here." >&2
  echo >&2
  echo "If you did mean to run locally, clear the production variables:" >&2
  echo >&2
  echo "  env -u DJANGO_CONFIGURATION -u ENVIRONMENT -u DBHOST \\" >&2
  echo "      -u DBNAME -u DBUSER -u DBPASSWORD ./run_local.sh" >&2
  echo >&2
  exit 1
}

if [ "${DJANGO_CONFIGURATION:-}" = "Prod" ]; then
  refuse_production "DJANGO_CONFIGURATION=Prod"
fi
if [ "${ENVIRONMENT:-}" = "Prod" ]; then
  refuse_production "ENVIRONMENT=Prod"
fi
if [ -n "${DBHOST:-}" ]; then
  refuse_production "DBHOST"
fi

# Second half of the belt and braces: pin the configuration for every
# manage.py call below rather than trusting manage.py's setdefault, and drop
# ENVIRONMENT so its fallback cannot reintroduce Prod.
export DJANGO_CONFIGURATION=Dev
unset ENVIRONMENT

if [ ! -f "cert.pem" ]; then
  if ! command -v mkcert &> /dev/null; then
    apt-get install -y mkcert
  fi
  mkcert -cert-file cert.pem -key-file key.pem localhost 127.0.0.1
fi
# A fresh checkout starts with an empty database, so without these the
# catalogue renders empty, the homepage has no highlights and every product
# page 404s -- which reads like a broken app rather than an unseeded one.
# Mirrors what scripts/start-server.sh does on the primary in production, so
# local startup and deployed startup do not drift.
#
# Both are safe to run on every start *against the local sqlite database that
# the forced Dev configuration selects*. `migrate` is a no-op once applied,
# and seed_products upserts: it writes new rows with bulk_create() and
# existing ones with a queryset .update(), so a second run reports everything
# as updated rather than failing on a duplicate primary key.
#
# Neither needs Stripe credentials. Both of those write paths deliberately
# bypass Product.save(), which is what would otherwise call Stripe to mint a
# product id for every row; ids are minted lazily on first add-to-cart
# instead. So this works offline and with no keys in the environment.
python manage.py migrate
python manage.py seed_products
python manage.py runserver_plus --cert-file cert.pem --key-file key.pem
