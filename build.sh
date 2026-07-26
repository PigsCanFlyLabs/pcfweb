#!/bin/bash
set -euo pipefail
set -x
# Re-sync main/static/assets/images out of the sibling pcfweb-assets checkout
# and run the three guards over what landed: the per-file size ceiling, the
# Git LFS pointer check, and the fixture-references-real-files check. Same
# steps in the same order as when they were inline here, and each one is still
# fatal -- the script's default mode is fatal precisely so that this call site
# needs no flag to stay strict.
#
# Shared with run_local.sh, which passes --warn, so the deploy path and the
# local path cannot drift. The local path had no copy step at all, which is how
# a checkout could sit on a stale image tree from a build months earlier; see
# the header of scripts/sync-local-assets.sh.
./scripts/sync-local-assets.sh

# Digital book archives, from the sibling pcfweb-book-assets checkout (Git
# LFS). These are the files paying customers get emailed a link to, so every
# one is validated -- LFS pointer, size, ZIP magic -- before it can reach the
# image. The guard fails the build loudly; see scripts/check-book-assets.sh
# and the README in pcfweb-book-assets.
./scripts/check-book-assets.sh "${BOOK_ASSETS_DIR:-../pcfweb-book-assets}" book-assets

./scripts/checks.sh
./scripts/check-image-assets.sh static/assets/images "collected static image assets"
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
