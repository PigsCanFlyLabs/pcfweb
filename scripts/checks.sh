#!/bin/bash
# Shared check pipeline, run by both CI (.github/workflows/ci.yml) and the
# deploy script (build.sh) so the two can't drift. Assumes dependencies are
# installed; run from the repo root.
set -ex
# Invoke mypy through the active interpreter, not a bare `mypy` that may resolve
# to a separate pipx/uv/--user install. mypy loads plugins under its own
# interpreter, and configurations_mypy_django_plugin imports django (and every
# app in INSTALLED_APPS) at startup -- so mypy must share the venv where the
# project deps live, or plugin import fails with "No module named 'django'".
python -m mypy -p main -p pigscanfly
# Migrations must be committed, not generated at deploy time.
./manage.py makemigrations --check --dry-run
# Tests render pages that thumbnail collected static assets, so collect first.
./manage.py collectstatic --no-input
# Materialise every thumbnail into STATIC_ROOT, i.e. into the tree the
# Dockerfile COPYs. Without this the first request on each pod generates them
# instead, into a directory deploy.yaml mounts no volume over -- so they were
# per-pod and per-boot, and the image request round-robined onto a replica that
# did not have the file. See main/management/commands/pregenerate_thumbnails.py.
#
# Before the tests, not after: main/tests/test_static_thumbnails.py asserts the
# pages reference files that are already on disk, which is only a real
# assertion if something other than the test put them there.
#
# --allow-absent-asset-tree because CI (.github/workflows/ci.yml) checks out
# this repository on its own, and the covers live in the sibling pcfweb-assets
# checkout it does not have. The flag is all-or-nothing: it skips the covers
# only when NOT ONE of them is present, prints a banner saying so, and still
# fails on a tree that exists with anything wrong in it. It does not weaken the
# artifact -- build.sh runs `pregenerate_thumbnails --check` without it, on the
# tree about to be COPYd into the image, and that is the seal that protects
# production.
./manage.py pregenerate_thumbnails --allow-absent-asset-tree
./manage.py test main
./manage.py validate_templates --ignore-app newsletter
# Kubernetes manifests parse.
python3 -c "import yaml; list(yaml.safe_load_all(open('pg-bootstrap.yaml'))); list(yaml.safe_load_all(open('deploy.yaml')))"
