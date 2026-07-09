#!/usr/bin/env bash
set -ex
# start-server.sh
cd /opt/app
export DJANGO_CONFIGURATION=${DJANGO_CONFIGURATION:-"Prod"}
export DJANGO_SETTINGS_MODULE=${DJANGO_SETTINGS_MODULE:-"pigscanfly.settings"}
# Run migrations + load the bootstrap fixtures on the primary only
if [ ! -z "$PRIMARY" ]; then
  ./manage.py migrate
  ./manage.py loaddata initial_products
fi
# Start gunicorn
gunicorn pigscanfly.wsgi --user www-data --bind 0.0.0.0:8010 --workers 4 2>&1 | grep -v kube-probe &
# And nginx to proxy & serve static files
nginx -g "daemon off;" 2>&1 |grep -v kube-proxy
