#!/bin/bash
# Proof-of-non-vacuity for scripts/check-local-runtime.sh
#
# This test demonstrates that the guard:
# 1. Passes when prerequisites are met (logo and thumbnails exist)
# 2. Fails naming the specific missing file when logo is absent
# 3. Fails naming the problem when thumbnails are absent
#
# Run after a successful run_local.sh.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

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
LOGO="$STATIC_ROOT/assets/logo-cropped.png"
IMAGES="$STATIC_ROOT/assets/images"

echo "=== Non-vacuity test for scripts/check-local-runtime.sh ==="
echo

# Make the guard script executable if it's not already
if [ ! -x scripts/check-local-runtime.sh ]; then
  chmod +x scripts/check-local-runtime.sh
fi

# Baseline: should pass after run_local.sh has run
echo "Test 1: Guard should PASS when STATIC_ROOT is properly populated"
if ./scripts/check-local-runtime.sh; then
  echo "PASS: Guard passed with complete STATIC_ROOT"
else
  echo "FAIL: Guard failed when it should have passed"
  echo "  (Did run_local.sh complete successfully first?)"
  exit 1
fi
echo

# Test 2: Corrupt the logo and verify it fails naming the logo
if [ -f "$LOGO" ]; then
  echo "Test 2: Guard should FAIL and NAME logo-cropped.png when logo is corrupt"
  cp -p "$LOGO" "$LOGO.backup"
  echo "not a PNG" > "$LOGO"

  set +e
  output=$(./scripts/check-local-runtime.sh 2>&1)
  status=$?
  set -e
  printf '%s\n' "$output"
  if [ "$status" -ne 0 ] && printf '%s\n' "$output" | grep -q "assets/logo-cropped.png"; then
    echo "PASS: Guard detected corrupt logo and named it"
  else
    echo "FAIL: Guard did not detect or did not name corrupt logo"
    mv "$LOGO.backup" "$LOGO"
    exit 1
  fi

  # Restore
  mv "$LOGO.backup" "$LOGO"
  echo
else
  echo "SKIP Test 2: $LOGO does not exist"
  echo "  Run ./run_local.sh first to populate STATIC_ROOT"
  exit 1
fi

# Test 3: Delete thumbnails and verify it fails naming the missing thumbnails
if [ -d "$IMAGES" ]; then
  echo "Test 3: Guard should FAIL and NAME a missing thumbnail when it's deleted"
  victim=$(find "$IMAGES" -type f -name '*.290x380_q85.jpg' | sort | head -1)
  if [ -z "$victim" ]; then
    echo "FAIL: no book cover thumbnail found under $IMAGES"
    exit 1
  fi
  mv "$victim" "$victim.backup"

  set +e
  output=$(./scripts/check-local-runtime.sh 2>&1)
  status=$?
  set -e
  printf '%s\n' "$output"
  if [ "$status" -ne 0 ] && printf '%s\n' "$output" | grep -q "$victim"; then
    echo "PASS: Guard detected missing thumbnail and named it"
  else
    echo "FAIL: Guard did not detect or did not name missing thumbnail"
    mv "$victim.backup" "$victim"
    exit 1
  fi

  # Restore
  mv "$victim.backup" "$victim"
  echo
fi

echo "Test 4: Guard should PASS again after restores"
if ./scripts/check-local-runtime.sh; then
  echo "PASS: Guard passed after restoring the induced failures"
else
  echo "FAIL: Guard failed after restoring the induced failures"
  exit 1
fi
echo

echo "=== All non-vacuity tests passed ==="
echo "The guard actively inspects files and fails loudly when they are missing or corrupt."
