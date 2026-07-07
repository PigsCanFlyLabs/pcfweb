# pcfweb

The Django app behind [www.pigscanfly.ca](https://www.pigscanfly.ca) — the
Pigs Can Fly Labs site and store.

## Local development

Requires Python 3.13 (matching the Docker image; 3.10+ works).

```bash
python3.13 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
# cal-sync-magic is a private repo, installed from a sibling checkout in the
# Docker image; it's optional (the calendar app disables itself when it's
# missing). For calendar work, clone it next to this repo or:
pip install git+https://github.com/holdenk/cal-sync-magic.git

./manage.py migrate
./manage.py loaddata initial_products
./run_local.sh          # runserver_plus with TLS via mkcert
```

The `Dev` configuration (sqlite, file-based email in `sent_emails/`) is the
default; set `ENVIRONMENT=Prod` (or `DJANGO_CONFIGURATION=Prod`) for the
production settings class.

Checks — one script shared by local dev, `build.sh`, and GitHub Actions
(`.github/workflows/ci.yml`):

```bash
./scripts/checks.sh
```

## Environment variables

| Variable | Used by | Notes |
| --- | --- | --- |
| `SECRET_KEY` | Prod (required), Base (optional) | Base falls back to an insecure dev-only value. |
| `STRIPE_TEST_SECRET_KEY` | Dev / Base | Test-mode Stripe key. |
| `STRIPE_LIVE_SECRET_KEY` | Prod | Live Stripe key. |
| `DBHOST` / `DBNAME` / `DBUSER` / `DBPASSWORD` | Prod | Postgres connection; wired in `deploy.yaml` to the in-cluster DB. |
| `EMAIL_HOST` / `EMAIL_HOST_USER` / `EMAIL_HOST_PASSWORD` | Prod | SMTP. |
| `GOOGLE_CLIENT_SECRETS_FILE` / `GOOGLE_CLIENT_SECRETS_TEXT` | cal-sync-magic | Calendar sync; optional, warns when absent. |
| `MAXMIND_LICENSE_KEY` | build.sh (image build) | Bundles the GeoLite2 country DB for region-specific buy links; optional. |
| `GEOIP_PATH` | all | Directory holding `GeoLite2-Country.mmdb`; defaults to `<repo>/geoip` (set to `/opt/app/geoip` in the image). |

> **Note:** a Stripe *test* key and a mkcert dev key were committed to this
> repo's history in the past. Both should be treated as burned — rotate the
> Stripe test key in the dashboard; the settings now only read keys from the
> environment.

## Products / fixtures

Bootstrap products (Holden's books) live in
`main/fixtures/initial_products.yaml`. Rules:

- Fixture rows use **pks 100+** so they never collide with rows created
  directly in prod.
- The primary pod re-runs `loaddata initial_products` on **every deploy**, so
  admin edits to those pks get overwritten — edit the YAML instead.
- `external_product_id` (Stripe) is generated lazily on first add-to-cart,
  so loading fixtures needs no Stripe access.

### Region-specific buy links

Books carry `amazon_link` and `bookshop_link` (shown to everyone) plus
`amazon_in_link` / `flipkart_link`, which are shown first to visitors whose
IP resolves to India via MaxMind GeoLite2. Country detection needs
`GeoLite2-Country.mmdb` in `GEOIP_PATH` — the Docker build downloads it when
`MAXMIND_LICENSE_KEY` is set (passed as a BuildKit secret so it stays out of
the image history). Without the database the site quietly serves the default
links only.

## Deploying

`./build.sh` is the whole pipeline: mypy → migration check → tests →
template validation → collectstatic (assets are copied in from a sibling
`pcfweb-assets` checkout) → multi-arch Docker build/push
(`holdenk/pcfweb:<tag>`) → `kubectl apply`.

The Kubernetes objects:

- `pg-bootstrap.yaml` — the database: a [CloudNativePG](https://cloudnative-pg.io/)
  `Cluster` (3 instances, 10Gi `encrypted-local-path` storage, nightly
  backups to B2), plus manual `Backup` and nightly `ScheduledBackup`.
- `deploy.yaml` — the app: `web-primary` (1 replica; runs `migrate` +
  `loaddata` on start), `web` (3 replicas), `web-svc`, and the ingress for
  `www.pigscanfly.ca`.

The app reaches Postgres through the operator-created `pcfweb-pg-rw`
Service; `DBHOST`/`DBNAME`/`DBUSER` are set directly in `deploy.yaml` and
`DBPASSWORD` comes from the `pcfweb-internal-pg-secret` Secret.

### One-time cluster prerequisites (not in this repo)

1. CloudNativePG operator installed cluster-wide.
2. The `encrypted-local-path` StorageClass.
3. Secrets in the `pcfweb` namespace:
   - `pcfweb-superuser-pg-secret` — `kubernetes.io/basic-auth`,
     username `postgres` + password.
   - `pcfweb-internal-pg-secret` — `kubernetes.io/basic-auth`,
     username `pigscanfly` + password (the app role).
   - `pg-backup` — `PG_ACCESS_KEY_ID` / `PG_ACCESS_SECRET_KEY` for the
     `pcfweb-pg-backup` B2 bucket (use a bucket dedicated to pcfweb).
   - `pcfweb-secret` — the app env (SECRET_KEY, Stripe, email, …).
   - `client-secret` — the Google OAuth client json volume.

### One-time MySQL → Postgres data migration

The site previously ran against an external MySQL. To carry data over:

1. From a checkout of the last pre-Postgres revision (which still has the
   MySQL settings) with access to the old DB:
   `./manage.py dumpdata --natural-foreign -e contenttypes -e auth.Permission -e sessions -o prod-dump.json`
2. On this revision: `kubectl -n pcfweb port-forward svc/pcfweb-pg-rw 5432:5432`,
   set `DBHOST=127.0.0.1` etc., then `./manage.py migrate` and
   `./manage.py loaddata prod-dump.json`.
3. Reset sequences (explicit-pk loads don't advance them):
   `./manage.py sqlsequencereset main auth | kubectl -n pcfweb exec -i pcfweb-pg-1 -- psql -U postgres pigscanfly`
