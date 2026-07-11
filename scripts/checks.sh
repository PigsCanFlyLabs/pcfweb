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
./manage.py test main
./manage.py validate_templates --ignore-app newsletter
# Kubernetes manifests parse.
python3 -c "import yaml; list(yaml.safe_load_all(open('pg-bootstrap.yaml'))); list(yaml.safe_load_all(open('deploy.yaml')))"
