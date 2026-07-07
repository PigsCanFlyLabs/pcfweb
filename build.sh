#!/bin/bash
set -ex
mypy -p main -p pigscanfly
# Migrations must be committed, not generated at deploy time.
./manage.py makemigrations --check --dry-run
./manage.py migrate
./manage.py loaddata initial_products
# Hack, for now.
rm -rf ./cal-sync-magic
cp -af ../cal-sync-magic ./
rm -rf main/static/assets/images
cp -af ../pcfweb-assets/images main/static/assets/
# Tests render pages that thumbnail collected static assets, so collect first.
./manage.py collectstatic --no-input
./manage.py test main
./manage.py validate_templates --ignore-app newsletter
# Bundle the GeoLite2 country DB when a MaxMind key is available (used for
# the India-specific buy links); the image builds fine without it.
MAXMIND_SECRET_ARGS=()
if [ -n "${MAXMIND_LICENSE_KEY:-}" ]; then
  MAXMIND_SECRET_ARGS=(--secret id=maxmind,env=MAXMIND_LICENSE_KEY)
fi
docker buildx build --platform=linux/amd64,linux/arm64 "${MAXMIND_SECRET_ARGS[@]}" -t holdenk/pcfweb:v0.10.0 . --push
# Deploy the database first (CloudNativePG cluster), then the app.
kubectl apply -f pg-bootstrap.yaml
kubectl wait --for=condition=Ready cluster/pcfweb-pg -n pcfweb --timeout=600s || true
kubectl apply -f deploy.yaml
