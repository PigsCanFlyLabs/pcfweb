#!/bin/bash
set -euo pipefail
set -x
# Hack, for now. Only pcfweb-assets/images is copied -- that repo also keeps
# an originals/ directory of full-resolution masters, which deliberately does
# not ship.
rm -rf main/static/assets/images
cp -af ../pcfweb-assets/images main/static/assets/

# Everything copied above lands in the image and is then duplicated by
# collectstatic, in an artifact Kubernetes re-pulls on every rollout. The
# per-file budget is 2MB (see pcfweb-assets/README.md); this is the hard
# ceiling that catches a master committed to images/ by mistake, which is how
# a single 50MB panorama used to ship.
ASSET_MAX_BYTES=5000000
oversized=$(find main/static/assets/images -type f -size +${ASSET_MAX_BYTES}c \
  -printf '%s\t%p\n' | sort -rn || true)
if [ -n "$oversized" ]; then
  set +x
  echo >&2
  echo "ERROR: image assets over $((ASSET_MAX_BYTES / 1000000))MB:" >&2
  echo "$oversized" | awk -F'\t' '{printf "  %6.1fMB  %s\n", $1/1000000, $2}' >&2
  echo >&2
  echo "Put the master in pcfweb-assets/originals/ and a resized copy in" >&2
  echo "pcfweb-assets/images/ under the same name." >&2
  exit 1
fi
./scripts/check-image-assets.sh main/static/assets/images

./scripts/checks.sh
# deploy.yaml is the single source of truth for the image tag.
TAG=$(grep -oE 'holdenk/pcfweb:[A-Za-z0-9._-]+' deploy.yaml | head -1)

# Pushing over a tag that is already deployed is a silent no-op: `kubectl
# apply` sees an unchanged Deployment spec, so no rollout is triggered and the
# running pods keep the old image regardless of imagePullPolicy. Refuse to
# build until the tag in deploy.yaml has been bumped past what the cluster is
# running.
#
# This fails closed. Swallowing a kubectl error would leave RUNNING_TAGS empty,
# the comparison below would find no match, and the build would push over a
# tag that is in fact running -- reintroducing exactly the silent stale deploy
# this guard exists to catch, and only when something is already wrong. An
# empty result on a *successful* lookup is different and fine: it means
# nothing is deployed yet.
if ! RUNNING_TAGS=$(kubectl get deploy -n pcfweb \
      -o jsonpath='{.items[*].spec.template.spec.containers[*].image}' 2>&1); then
  set +x
  echo >&2
  echo "ERROR: could not read the running image tags from the cluster:" >&2
  echo "  ${RUNNING_TAGS}" >&2
  echo >&2
  echo "Refusing to build: without knowing what is deployed, pushing ${TAG}" >&2
  echo "risks overwriting a running tag and rolling nothing out. Fix cluster" >&2
  echo "access (kubectl config current-context) and re-run." >&2
  exit 1
fi
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
