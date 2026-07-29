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

# ---------------------------------------------------------------------------
# Re-sync the image assets on every single run.
#
# main/static/assets/images is a derived copy of ../pcfweb-assets/images and is
# gitignored, so `git pull` never touches it and, before this, only build.sh
# ever refreshed it. A checkout that last ran build.sh before PR #23 moved the
# book covers into images/book_covers/ therefore has a *full* image directory
# that is still wrong -- flat cover files, no book_covers/ subdirectory, plus
# orphans for retired products -- so every book cover 404s while the banners
# and the logo render fine.
#
# That is why this is a plain unconditional call and not an `if [ ! -d ... ]`:
# the directory exists, so a guarded copy would fix precisely nothing. The
# script's rm -rf is the part that matters.
#
# Missing pcfweb-assets is the one legitimate local exception: a developer can
# choose not to clone the sibling checkout and accept broken product images.
# If that checkout is present, though, every problem in it is a genuine fault
# and stays fatal.
# ---------------------------------------------------------------------------
asset_tree_absent=no
source_images="${ASSETS_DIR:-../pcfweb-assets}/images"
if [ -d "$source_images" ]; then
  ./scripts/sync-local-assets.sh
else
  asset_tree_absent=yes
fi

# ---------------------------------------------------------------------------
# Collect static files and pre-generate thumbnails.
#
# Templates reference static assets (logo-cropped.png, CSS, JS) and product
# covers through STATIC_URL, which resolves them via STATIC_ROOT, not via the
# staticfiles finders. So a file that is committed (like logo-cropped.png) or
# synced above (like book covers) is still invisible until collectstatic copies
# it into STATIC_ROOT.
#
# Thumbnails are generated into STATIC_ROOT at build time. The thumbnail engine
# resolves SOURCE files through STATIC_ROOT too, so a cover that exists but was
# not collected produces 'The source file does not appear to be an image' (which
# actually means 'not found at STATIC_ROOT'), not a resize.
#
# --allow-absent-asset-tree is passed only when the sibling pcfweb-assets
# checkout is actually absent. If the tree exists, a missing, stale, pointer or
# corrupt cover stays fatal.
#
# This is the same flow as build.sh (which calls scripts/checks.sh), minus mypy
# and tests. Keeping local and deploy in step means a run_local.sh success
# predicts a build.sh success.
# ---------------------------------------------------------------------------
python manage.py collectstatic --no-input
if [ "$asset_tree_absent" = yes ]; then
  python manage.py pregenerate_thumbnails --allow-absent-asset-tree
else
  python manage.py pregenerate_thumbnails
fi

# The digital-download archives, from the sibling pcfweb-book-assets checkout.
# Warn rather than refuse: that repo is Git LFS with a multi-megabyte ZIP per
# book, so a machine that never touches fulfilment legitimately does not have
# it, and the only local consequence is that the e-book download 404s. build.sh
# keeps this one fatal, where the consequence is a paying customer being
# emailed a 130-byte pointer stub -- same guard script, two severities, no
# second copy of the logic.
if ! ./scripts/check-book-assets.sh \
      "${BOOK_ASSETS_DIR:-../pcfweb-book-assets}" book-assets; then
  set +x
  echo >&2
  echo "WARNING: no usable book archives (see the error just above)." >&2
  echo "The site will start; digital e-book downloads will 404 locally." >&2
  echo "To fix, beside this checkout:" >&2
  echo "  git clone https://github.com/PigsCanFlyLabs/pcfweb-book-assets.git" >&2
  echo "  cd pcfweb-book-assets && git lfs install && git lfs pull" >&2
  echo >&2
  set -x
fi

# Verify immediately before the server starts so this is the last local-site
# asset status a human sees. The absent pcfweb-assets path is allowed but loud;
# every problem in a present asset tree remains fatal.
if [ "$asset_tree_absent" = yes ]; then
  ./scripts/check-local-runtime.sh --allow-absent-asset-tree
else
  ./scripts/check-local-runtime.sh
fi

# Let main.urls serve /static/ from STATIC_ROOT. The default staticfiles
# handler searches source static directories instead, so it cannot see the
# generated thumbnails that pregenerate_thumbnails writes into STATIC_ROOT.
# Any optional addrport argument is passed through to runserver_plus.
python manage.py runserver_plus --nostatic --cert-file cert.pem --key-file key.pem "$@"
