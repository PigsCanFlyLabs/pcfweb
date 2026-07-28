#!/bin/bash
# Verify that run_local.sh's prerequisites are met: collectstatic ran and
# populated STATIC_ROOT with the committed logo, and pregenerate_thumbnails ran
# and produced thumbnail files for the book covers.
#
# This is a non-vacuity guard: it must fail LOUDLY when the preconditions are
# not met, naming what is actually missing. A guard that passes over a tree it
# never inspected -- or that is satisfied by a directory merely existing -- is
# the exact failure mode this exists to prevent.
#
# Exit codes:
#   0  checks passed
#   1  a check failed (named on stderr)
#   2  usage error
#
# Usage: scripts/check-local-runtime.sh [--warn]
#
# With --warn, problems are reported loudly but the exit code is 0, so the
# caller can let the server start anyway. Without it (the default), the first
# problem exits 1.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

mode=fatal
case "${1:-}" in
  "") ;;
  --warn) mode=warn ;;
  *)
    echo "usage: ${0##*/} [--warn]" >&2
    exit 2
    ;;
esac

STATIC_ROOT="${STATIC_ROOT:-staticfiles}"
problems=0

problem() {
  local headline="$1"
  local severity="ERROR"
  if [ "$mode" = warn ]; then
    severity="WARNING"
  fi

  echo >&2
  echo "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!" >&2
  echo "!! ${severity}: ${headline}" >&2
  echo "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!" >&2
  cat >&2
  echo >&2

  problems=$((problems + 1))
  if [ "$mode" = fatal ]; then
    exit 1
  fi
}

# --- 1. collectstatic must have run and copied the logo --------------------
# logo-cropped.png is the masthead logo and is committed in the repo (not from
# the sibling pcfweb-assets). If it is missing from STATIC_ROOT, collectstatic
# did not run.
#
# This is the discriminator: if the logo is missing but book covers are fine,
# collectstatic ran and sync-local-assets.sh did not; if the logo is fine but
# covers are missing, sync-local-assets.sh ran and collectstatic did not. Here
# we are checking the latter: that collectstatic ran.
LOGO="$STATIC_ROOT/assets/logo-cropped.png"
if [ ! -f "$LOGO" ]; then
  problem "collectstatic has not run or STATIC_ROOT is missing" <<EOF
Expected the committed logo at:
  $LOGO

That file is committed in main/static/assets/logo-cropped.png and is copied to
STATIC_ROOT by 'python manage.py collectstatic'. Templates resolve static
assets through STATIC_ROOT, so without it the masthead renders broken.

If STATIC_ROOT ($STATIC_ROOT) exists but is empty, collectstatic did not run.
If STATIC_ROOT does not exist at all, run_local.sh's migrate + seed_products
step failed and the server will not start anyway.
EOF
elif [ ! -s "$LOGO" ]; then
  problem "logo exists but is empty" <<EOF
$LOGO is zero bytes. collectstatic copied it, but the source was empty.
EOF
else
  # Verify it is actually an image, not a Git LFS pointer or random text.
  # PNG magic: \x89PNG\r\n\x1a\n (bytes 0-7)
  if ! head -c 8 "$LOGO" | grep -q $'^\x89PNG\r\n\x1a\n'; then
    problem "logo is not a PNG image" <<EOF
$LOGO exists but does not start with the PNG magic bytes. It may be a Git LFS
pointer stub (130 bytes of 'version https://git-lfs.github.com/spec/v1') or
corrupt.

First 100 bytes:
$(head -c 100 "$LOGO" | od -A x -t x1z -v | head -5)
EOF
  fi
fi

# --- 2. pregenerate_thumbnails must have produced thumbnail files ----------
# Book covers are synced from pcfweb-assets and then thumbnailed into
# STATIC_ROOT. If STATIC_ROOT has zero .jpg files under assets/images/, either
# sync-local-assets.sh did not run (so no covers were copied) or
# pregenerate_thumbnails did not run (so covers exist but were never resized).
#
# This check distinguishes "no thumbnails at all" from "covers exist but
# thumbnails do not": if covers are in main/static/assets/images but none in
# STATIC_ROOT, pregenerate_thumbnails did not run.
if [ -d "$STATIC_ROOT/assets/images" ]; then
  thumbnail_count=$(find "$STATIC_ROOT/assets/images" -type f \( -name '*.jpg' -o -name '*.png' \) | wc -l | tr -d ' ')
  if [ "$thumbnail_count" -eq 0 ]; then
    # Check if source covers exist in main/static/assets/images
    if [ -d "main/static/assets/images/book_covers" ]; then
      source_count=$(find main/static/assets/images/book_covers -type f \( -name '*.jpg' -o -name '*.png' \) | wc -l | tr -d ' ')
      if [ "$source_count" -gt 0 ]; then
        problem "pregenerate_thumbnails did not run" <<EOF
Found $source_count cover image(s) in main/static/assets/images/book_covers, but
zero image files in $STATIC_ROOT/assets/images.

sync-local-assets.sh populated the source tree, but 'python manage.py
pregenerate_thumbnails' did not run to materialize them into STATIC_ROOT. Book
cover thumbnails will 404.
EOF
      else
        problem "no book covers synced from pcfweb-assets" <<EOF
main/static/assets/images/book_covers exists but contains no images.
sync-local-assets.sh either did not run or the sibling pcfweb-assets checkout
is empty/missing.
EOF
      fi
    else
      problem "no book covers synced from pcfweb-assets" <<EOF
main/static/assets/images/book_covers does not exist. sync-local-assets.sh
either did not run or the sibling pcfweb-assets checkout is missing.
EOF
    fi
  fi
else
  problem "STATIC_ROOT assets missing" <<EOF
$STATIC_ROOT/assets/images does not exist. collectstatic did not run, or
STATIC_ROOT is a completely empty directory.
EOF
fi

# --- 3. verdict ------------------------------------------------------------
if [ "$problems" -gt 0 ]; then
  if [ "$mode" = warn ]; then
    echo "!! ${problems} runtime check(s) failed (see above). Starting anyway." >&2
    echo "!! Pages referencing these assets will render broken." >&2
    echo >&2
    exit 0
  fi
  # fatal mode already exited at the first problem, so unreachable
fi

echo "Runtime checks passed: logo and thumbnails are in $STATIC_ROOT"
