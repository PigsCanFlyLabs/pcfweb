#!/bin/bash
set -ex
# Hack, for now.
rm -rf ./cal-sync-magic
cp -af ../cal-sync-magic ./
rm -rf main/static/assets/images
cp -af ../pcfweb-assets/images main/static/assets/
./scripts/checks.sh
# deploy.yaml is the single source of truth for the image tag.
TAG=$(grep -oE 'holdenk/pcfweb:[A-Za-z0-9._-]+' deploy.yaml | head -1)
# Bundle the GeoLite2 country DB when a MaxMind key is available (used for
# the India-specific buy links); the image builds fine without it.
MAXMIND_SECRET_ARGS=()
if [ -n "${MAXMIND_LICENSE_KEY:-}" ]; then
  MAXMIND_SECRET_ARGS=(--secret id=maxmind,env=MAXMIND_LICENSE_KEY)
fi
docker buildx build --platform=linux/amd64,linux/arm64 "${MAXMIND_SECRET_ARGS[@]}" -t "$TAG" . --push
# Deploy the database first (CloudNativePG cluster), then the app.
kubectl apply -f pg-bootstrap.yaml
kubectl wait --for=condition=Ready cluster/pcfweb-pg -n pcfweb --timeout=600s || true
kubectl apply -f deploy.yaml
