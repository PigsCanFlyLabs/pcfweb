#!/bin/bash
# Re-sync main/static/assets/images out of the sibling pcfweb-assets checkout,
# then run every guard over what landed.
#
# Usage: scripts/sync-local-assets.sh [--warn]
#
# Called by build.sh (no flag: every check fatal) and by run_local.sh (--warn:
# every check reports loudly and the caller carries on). One implementation,
# two severities -- deliberately not two scripts. This repo has already lost
# real time to check.sh silently diverging from scripts/checks.sh, and the
# copy below is the step whose absence from run_local.sh is the bug this
# script exists to close.
#
# Exit codes:
#   0  synced, every check passed
#   1  a check failed (the default, fatal mode; exits at the first one)
#   2  usage error
#   3  --warn only: problems were reported but not enforced
#
# Fatal is the DEFAULT so that omitting the flag fails closed. A caller that
# forgets --warn gets the strict behaviour, never the lax one, and relaxing a
# guard takes an explicit word at the call site where a reviewer will see it.
# Note for anyone adding a third caller: under `set -e` an exit 3 aborts, so
# handle it the way run_local.sh does rather than assuming --warn returns 0.
#
# ---------------------------------------------------------------------------
# THE FAILURE THIS EXISTS TO STOP IS STALENESS, NOT ABSENCE.
#
# main/static/assets/images is a derived copy and is gitignored (.gitignore
# line 10), so git never updates it and, until now, nothing outside build.sh
# ever refreshed it. A checkout whose last build.sh run predates PR #23 --
# which relocated every book cover into pcfweb-assets images/book_covers/ --
# has an images/ directory that is *full* and nevertheless wrong:
#
#   stale tree                        pcfweb-assets (current)
#     learning_spark_1ed.jpg   flat     images/book_covers/learning_spark_1ed.jpg
#     high_performance_spark.jpg flat   images/book_covers/high_performance_spark.jpg
#     book_covers/  ABSENT              images/book_covers/distributed_computing_4_kids.jpg
#     spacebeaver-logo.png              (gone: product retired)
#     transit-large.png                 (gone: service retired)
#
# The fixture asks for book_covers/learning_spark_1ed.jpg, so every book cover
# 404s -- while the banners and the logo, which are top-level and unchanged,
# render fine. That is why the symptom reads as a partial breakage rather than
# as a missing directory, and why it went unnoticed for so long.
#
# Consequences for the shape of this script:
#
#   * The sync is UNCONDITIONAL. `[ -d "$dest" ] || cp` would do nothing at
#     all for a stale tree, because the directory is there.
#   * The sync is DESTRUCTIVE. The rm -rf is what removes the orphaned
#     spacebeaver/transit files and the flat cover copies; a merge-style
#     `cp -a` over the top would leave them behind forever.
# ---------------------------------------------------------------------------
set -euo pipefail

# Run from the repo root so the ./scripts/* calls below resolve, whatever the
# caller's working directory is.
cd "$(dirname "${BASH_SOURCE[0]}")/.."

# Test seams. The defaults are the real paths; main/tests/test_sync_local_assets.py
# points them at temporary directories so the guards can be exercised without
# a build and without touching the developer's own asset tree. Relative values
# are resolved from the repo root. Same idea as check-book-assets.sh taking its
# source and destination as arguments.
ASSETS_DIR="${ASSETS_DIR:-../pcfweb-assets}"
STATIC_ASSETS_DIR="${STATIC_ASSETS_DIR:-main/static/assets}"
PRODUCT_FIXTURE="${PRODUCT_FIXTURE:-main/fixtures/initial_products.yaml}"

# Only pcfweb-assets/images is copied. That repo also keeps an originals/
# directory of full-resolution masters, which deliberately does not ship.
source_images="$ASSETS_DIR/images"
dest="$STATIC_ASSETS_DIR/images"

# Everything copied here lands in the image and is then duplicated by
# collectstatic, in an artifact Kubernetes re-pulls on every rollout. The
# per-file budget is 2MB (see pcfweb-assets/README.md); this is the hard
# ceiling that catches a master committed to images/ by mistake, which is how a
# single 50MB panorama used to ship. Deliberately a constant and not an
# environment override: a guard nobody can loosen from a shell.
ASSET_MAX_BYTES=5000000

CLONE_HINT="git clone https://github.com/PigsCanFlyLabs/pcfweb-assets.git"

mode=fatal
case "${1:-}" in
  "") ;;
  --warn) mode=warn ;;
  *)
    echo "usage: ${0##*/} [--warn]" >&2
    exit 2
    ;;
esac
if [ "$#" -gt 1 ]; then
  echo "usage: ${0##*/} [--warn]" >&2
  exit 2
fi

BANNER="!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
problems=0

# problem <headline>, detail lines on stdin.
#
# In fatal mode this exits 1 at the first call, which preserves build.sh's
# original fail-fast ordering exactly. In warn mode it counts the problem and
# returns, so the developer is told about every one of them in a single run
# instead of discovering them one restart at a time.
problem() {
  local headline="$1"
  # Spelled out rather than `[ ... ] && severity=...`: a failing test as the
  # non-last command of an AND-list is exempt from `set -e`, which is true but
  # is not something a reader should have to know to trust this function.
  local severity="ERROR"
  if [ "$mode" = warn ]; then
    severity="WARNING"
  fi

  echo >&2
  echo "$BANNER" >&2
  echo "!! ${severity}: ${headline}" >&2
  echo "$BANNER" >&2
  cat >&2
  echo >&2

  problems=$((problems + 1))
  if [ "$mode" = fatal ]; then
    exit 1
  fi
}

# --- 1. is the sibling checkout even there? --------------------------------
synced=yes
if [ ! -d "$source_images" ]; then
  # realpath -m rather than "$(pwd)/$source_images": it normalises a path that
  # does not exist yet, and does not glue the working directory onto a value
  # that was already absolute.
  problem "the pcfweb-assets checkout is missing" <<EOF
Expected an images/ directory at:

  $(realpath -m "$source_images")

That repository is not vendored into this one; clone it beside this checkout:

  $CLONE_HINT $ASSETS_DIR

It uses Git LFS, so afterwards run 'git lfs install && git lfs pull' inside it.
EOF
  # Reached in warn mode only.
  synced=no
  echo "Leaving the existing $dest alone: deleting it would turn a stale" >&2
  echo "image tree into no image tree at all, which is strictly worse." >&2
  echo >&2
fi

# --- 2. the sync itself ----------------------------------------------------
if [ "$synced" = yes ]; then
  rm -rf "$dest"
  mkdir -p "$STATIC_ASSETS_DIR"
  cp -af "$source_images" "$STATIC_ASSETS_DIR/"
fi

# --- 3. guards over what actually landed -----------------------------------
# Skipped when there is no directory to look at; check-product-images.sh below
# reports that case on its own, and better, because it also names the pks.
if [ -d "$dest" ]; then
  oversized=$(find "$dest" -type f -size +${ASSET_MAX_BYTES}c \
    -printf '%s\t%p\n' | sort -rn || true)
  if [ -n "$oversized" ]; then
    problem "image assets over $((ASSET_MAX_BYTES / 1000000))MB" <<EOF
$(echo "$oversized" | awk -F'\t' '{printf "  %6.1fMB  %s\n", $1/1000000, $2}')

Put the master in pcfweb-assets/originals/ and a resized copy in
pcfweb-assets/images/ under the same name.
EOF
  fi

  # Git LFS pointer detection lives in check-image-assets.sh, which already
  # knows the sentinel and already names every offending file. Calling it is
  # the point: a second detector here would be one more thing to drift.
  if ! ./scripts/check-image-assets.sh "$dest" "source image assets"; then
    problem "the image assets are unmaterialised Git LFS pointers" <<EOF
scripts/check-image-assets.sh listed the pointer files above. They are ~130
byte text stubs, not images, so they copy and serve without complaint and
every page that uses one renders broken.

Fix it in the source checkout, not here -- this directory is overwritten on
every run:

  cd $ASSETS_DIR && git lfs install && git lfs pull
EOF
  fi
fi

# --- 4. does the fixture's view of the world match the files? --------------
# Runs unconditionally: it is the check that answers the question the
# developer actually has, which is "why is this product's image missing".
if ! ./scripts/check-product-images.sh "$PRODUCT_FIXTURE" "$dest"; then
  if [ "$problems" -eq 0 ]; then
    # Nothing else was wrong, so this is not a plumbing failure.
    problem "the product fixture references images that do not exist" <<EOF
scripts/check-product-images.sh listed the affected pks above.

This is a genuine content mismatch, not a stale copy: $ASSETS_DIR is present
and its files are real images, and the sync above already refreshed
$dest from it. So either the fixture names a path that was never added to
pcfweb-assets, or a cover moved there without main/fixtures/initial_products.yaml
being updated to follow it.
EOF
  else
    problem "the product fixture references images that do not exist" <<EOF
scripts/check-product-images.sh listed the affected pks above.

Expected, given the problem already reported above -- fix that first and
these should resolve with it.
EOF
  fi
fi

# --- 5. verdict ------------------------------------------------------------
if [ "$problems" -gt 0 ]; then
  # warn mode only; fatal mode exited at the first problem.
  echo "$BANNER" >&2
  echo "!! ${problems} asset problem(s) reported above, none of them fixed." >&2
  echo "!! Starting anyway. Pages using these images will render broken," >&2
  echo "!! so do not read a missing cover as a bug in the code." >&2
  echo "$BANNER" >&2
  echo >&2
  exit 3
fi

echo "Synced $(find "$dest" -type f | wc -l | tr -d ' ') image asset(s) from $source_images into $dest"
