#!/bin/bash
set -ex
mypy -p main -p pigscanfly
./manage.py migrate
./manage.py makemigrations
./manage.py migrate
./manage.py validate_templates --ignore-app newsletter
# Hack, for now.
rm -rf ./cal-sync-magic
cp -af ../cal-sync-magic ./
rm -rf main/static/assets/images
cp -af ../pcfweb-assets/images main/static/assets/
./manage.py collectstatic --no-input
docker buildx build --platform=linux/amd64,linux/arm64 -t holdenk/pcfweb:v0.9.10c . --push
kubectl apply -f deploy.yaml
