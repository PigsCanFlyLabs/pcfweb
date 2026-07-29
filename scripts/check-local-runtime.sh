#!/bin/bash
# Verify that run_local.sh's prerequisites are met: collectstatic ran and
# populated STATIC_ROOT with the committed logo and synced covers, and
# pregenerate_thumbnails ran and produced the files the templates request.
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
# Usage: scripts/check-local-runtime.sh [--allow-absent-asset-tree]
#
# --allow-absent-asset-tree is the same narrow exception as the management
# command's flag: it only downgrades a wholly absent cover tree, which means the
# sibling pcfweb-assets checkout is not present. Anything in a present tree that
# is missing, corrupt, stale, uncollected or unthumbnailed is still fatal.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

allow_absent_asset_tree=no
case "${1:-}" in
  "") ;;
  --allow-absent-asset-tree) allow_absent_asset_tree=yes ;;
  *)
    echo "usage: ${0##*/} [--allow-absent-asset-tree]" >&2
    exit 2
    ;;
esac
if [ "$#" -gt 1 ]; then
  echo "usage: ${0##*/} [--allow-absent-asset-tree]" >&2
  exit 2
fi

STATIC_ROOT=$(python - <<'PY'
import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "pigscanfly.settings")
os.environ.setdefault(
    "DJANGO_CONFIGURATION", os.environ.get("ENVIRONMENT", "Dev"))

import configurations.importer
configurations.importer.install()

from django.conf import settings

print(settings.STATIC_ROOT)
PY
)
COVER_ROOT="$STATIC_ROOT/assets/images"
BANNER="!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"

cover_tree_absent=yes
if [ -d "$COVER_ROOT" ]; then
  if find "$COVER_ROOT" -type f | grep -q .; then
    cover_tree_absent=no
  fi
fi

if [ "$allow_absent_asset_tree" = yes ] && [ "$cover_tree_absent" = yes ]; then
  echo >&2
  echo "$BANNER" >&2
  echo "!! WARNING: the pcfweb-assets checkout is absent." >&2
  echo "$BANNER" >&2
  echo "No product cover files were collected under:" >&2
  echo "  $COVER_ROOT" >&2
  echo >&2
  echo "The site will start, but pages using these files will render with broken images:" >&2
  python - <<'PY' >&2
import yaml

with open("main/fixtures/initial_products.yaml", "rb") as fh:
    entries = yaml.safe_load(fh) or []

names = sorted({
    str((entry.get("fields") or {}).get("image_name"))
    for entry in entries
    if isinstance(entry, dict)
    and entry.get("model") == "main.product"
    and (entry.get("fields") or {}).get("image_name")
})
for name in names:
    print(f"  assets/images/{name}")
PY
  echo >&2
fi

args=(pregenerate_thumbnails --check)
if [ "$allow_absent_asset_tree" = yes ]; then
  args+=(--allow-absent-asset-tree)
fi

python manage.py "${args[@]}"
echo "Runtime checks passed: generated static thumbnails are in $STATIC_ROOT"
