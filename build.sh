#!/bin/bash
set -euo pipefail
set -x
# Hack, for now.
rm -rf main/static/assets/images
cp -af ../pcfweb-assets/images main/static/assets/
./scripts/checks.sh
# deploy.yaml is the single source of truth for the image tag.
TAG=$(grep -oE 'holdenk/pcfweb:[A-Za-z0-9._-]+' deploy.yaml | head -1)

# Pushing over a tag that is already deployed is a silent no-op: `kubectl
# apply` sees an unchanged Deployment spec, so no rollout is triggered and the
# running pods keep the old image regardless of imagePullPolicy. Refuse to
# build until the tag in deploy.yaml has been bumped past what the cluster is
# running.
RUNNING_TAGS=$(kubectl get deploy -n pcfweb -o jsonpath='{.items[*].spec.template.spec.containers[*].image}' 2>/dev/null || true)
for running in $RUNNING_TAGS; do
  if [ "$running" = "$TAG" ]; then
    set +x
    echo >&2
    echo "ERROR: deploy.yaml still pins ${TAG}, which is already running in the cluster." >&2
    echo "Bump the tag in deploy.yaml (all image: lines, including the" >&2
    echo "wait-for-migrations initContainer) before deploying, or this push" >&2
    echo "will overwrite the tag without ever rolling the pods." >&2
    exit 1
  fi
done

# Every image: reference in deploy.yaml must agree, or the initContainer and
# the app container can end up on different builds.
if [ "$(grep -cE 'image: holdenk/pcfweb:' deploy.yaml)" != "$(grep -cE "image: ${TAG}\$" deploy.yaml)" ]; then
  set +x
  echo "ERROR: deploy.yaml has holdenk/pcfweb image tags that disagree; they must all be ${TAG#*:}." >&2
  exit 1
fi
# Bundle the GeoLite2 country DB when a MaxMind key is available (used for
# the India-specific buy links); the image builds fine without it.
MAXMIND_SECRET_ARGS=()
if [ -n "${MAXMIND_LICENSE_KEY:-}" ]; then
  MAXMIND_SECRET_ARGS=(--secret id=maxmind,env=MAXMIND_LICENSE_KEY)
fi
docker buildx build --platform=linux/amd64,linux/arm64 "${MAXMIND_SECRET_ARGS[@]}" -t "$TAG" . --push
# Deploy the database first (CloudNativePG cluster), then the app. If the
# cluster never comes Ready there is no point applying the app -- it would
# just crashloop against an absent database -- so this failure is fatal
# rather than swallowed.
kubectl apply -f pg-bootstrap.yaml
kubectl wait --for=condition=Ready cluster/pcfweb-pg -n pcfweb --timeout=600s
kubectl apply -f deploy.yaml
kubectl rollout status deploy/web-primary -n pcfweb --timeout=300s
kubectl rollout status deploy/web -n pcfweb --timeout=300s
