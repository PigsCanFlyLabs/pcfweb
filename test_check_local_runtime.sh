#!/bin/bash
# Proof-of-non-vacuity for scripts/check-local-runtime.sh
#
# This test demonstrates that the guard:
# 1. Passes when prerequisites are met (logo and thumbnails exist)
# 2. Fails naming the specific missing file when logo is absent
# 3. Fails naming the problem when thumbnails are absent
#
# Run from repo root after a successful run_local.sh.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

echo "=== Non-vacuity test for scripts/check-local-runtime.sh ==="
echo

# Make the guard script executable if it's not already
if [ ! -x scripts/check-local-runtime.sh ]; then
  chmod +x scripts/check-local-runtime.sh
fi

# Baseline: should pass after run_local.sh has run
echo "Test 1: Guard should PASS when STATIC_ROOT is properly populated"
if ./scripts/check-local-runtime.sh; then
  echo "✓ PASS: Guard passed with complete STATIC_ROOT"
else
  echo "✗ FAIL: Guard failed when it should have passed"
  echo "  (Did run_local.sh complete successfully first?)"
  exit 1
fi
echo

# Test 2: Corrupt the logo and verify it fails naming the logo
if [ -f staticfiles/assets/logo-cropped.png ]; then
  echo "Test 2: Guard should FAIL and NAME logo-cropped.png when logo is corrupt"
  cp staticfiles/assets/logo-cropped.png staticfiles/assets/logo-cropped.png.backup
  echo "not a PNG" > staticfiles/assets/logo-cropped.png

  if ./scripts/check-local-runtime.sh 2>&1 | grep -q "logo is not a PNG image"; then
    echo "✓ PASS: Guard detected corrupt logo and named it"
  else
    echo "✗ FAIL: Guard did not detect or did not name corrupt logo"
    mv staticfiles/assets/logo-cropped.png.backup staticfiles/assets/logo-cropped.png
    exit 1
  fi

  # Restore
  mv staticfiles/assets/logo-cropped.png.backup staticfiles/assets/logo-cropped.png
  echo
else
  echo "✗ SKIP Test 2: staticfiles/assets/logo-cropped.png does not exist"
  echo "  Run ./run_local.sh first to populate STATIC_ROOT"
  exit 1
fi

# Test 3: Delete thumbnails and verify it fails naming the missing thumbnails
if [ -d staticfiles/assets/images ]; then
  echo "Test 3: Guard should FAIL and report missing thumbnails when they're deleted"
  mkdir -p staticfiles_backup
  mv staticfiles/assets/images staticfiles_backup/
  mkdir -p staticfiles/assets/images

  if ./scripts/check-local-runtime.sh 2>&1 | grep -q "pregenerate_thumbnails did not run"; then
    echo "✓ PASS: Guard detected missing thumbnails and reported the cause"
  else
    echo "✗ FAIL: Guard did not detect or did not report missing thumbnails"
    mv staticfiles_backup/images staticfiles/assets/
    rmdir staticfiles_backup 2>/dev/null || true
    exit 1
  fi

  # Restore
  mv staticfiles_backup/images staticfiles/assets/
  rmdir staticfiles_backup 2>/dev/null || true
  echo
fi

echo "=== All non-vacuity tests passed ==="
echo "The guard actively inspects files and fails loudly when they are missing or corrupt."
