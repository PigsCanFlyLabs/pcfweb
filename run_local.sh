#!/bin/bash
set -ex
if [ ! -f "cert.pem" ]; then
  if ! command -v mkcert &> /dev/null; then
    apt-get install -y mkcert
  fi
  mkcert -cert-file cert.pem -key-file key.pem localhost 127.0.0.1
fi
# A fresh checkout starts with an empty database, so without these the
# catalogue renders empty, the homepage has no highlights and every product
# page 404s -- which reads like a broken app rather than an unseeded one.
# Mirrors what scripts/start-server.sh does on the primary in production, so
# local startup and deployed startup do not drift.
#
# Both are safe to run on every start. `migrate` is a no-op once applied, and
# seed_products upserts: it writes new rows with bulk_create() and existing
# ones with a queryset .update(), so a second run reports everything as
# updated rather than failing on a duplicate primary key.
#
# Neither needs Stripe credentials. Both of those write paths deliberately
# bypass Product.save(), which is what would otherwise call Stripe to mint a
# product id for every row; ids are minted lazily on first add-to-cart
# instead. So this works offline and with no keys in the environment.
python manage.py migrate
python manage.py seed_products
python manage.py runserver_plus --cert-file cert.pem --key-file key.pem
