# pcfweb

The Django app behind [www.pigscanfly.ca](https://www.pigscanfly.ca) — the
Pigs Can Fly Labs site and store.

## Local development

Requires Python 3.13 (matching the Docker image; 3.10+ works).

```bash
python3.13 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt

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
| `STRIPE_LIVE_SECRET_KEY` | Prod (required) | Live Stripe key. Prod refuses to boot without it, rather than 500ing on the first customer to add to cart. |
| `STRIPE_WEBHOOK_SECRET` | Prod (required), all | Signing secret for `/stripe/webhook`. **Orders are never marked paid without it** — see [Orders](#orders) — so Prod refuses to boot without it too. |
| `STRIPE_SHIPPING_RATES` | all | Comma-separated Stripe shipping rate ids offered at checkout. Livemode-scoped: test-mode ids do not exist under a live key. Empty means no shipping options. |
| `STRIPE_TIMEOUT` | all | Seconds allowed for a Stripe call (default 15). Must stay below `GUNICORN_TIMEOUT` or a slow Stripe kills the worker instead of returning an error. |
| `GUNICORN_TIMEOUT` | Prod image | Worker timeout in seconds (default 60). |
| `ORDER_NOTIFICATION_EMAIL` | all | Where the "new paid order" mail goes; becomes `ADMINS`. Defaults to `support@pigscanfly.ca`. |
| `DBHOST` / `DBNAME` / `DBUSER` / `DBPASSWORD` | Prod | Postgres connection; wired in `deploy.yaml` to the in-cluster DB. |
| `EMAIL_HOST` / `EMAIL_HOST_USER` / `EMAIL_HOST_PASSWORD` | Prod | SMTP. |
| `MAXMIND_LICENSE_KEY` | build.sh (image build) | Bundles the GeoLite2 country DB for region-specific buy links; optional. |
| `GEOIP_PATH` | all | Directory holding `GeoLite2-Country.mmdb`; defaults to `<repo>/geoip` (set to `/opt/app/geoip` in the image). |

> **Note:** a Stripe *test* key and a mkcert dev key were committed to this
> repo's history in the past. Both should be treated as burned — rotate the
> Stripe test key in the dashboard; the settings now only read keys from the
> environment.

## Orders

A purchase is recorded as a PENDING `Order` (plus an `OrderItem` snapshot of
every cart line: name, unit price in cents, currency, quantity) *before* the
customer is sent to Stripe. The order id travels with the Checkout session as
`client_reference_id`, because by the time a webhook fires the cart is gone —
the success page empties it, and an anonymous cart is session-scoped.

`POST /stripe/webhook` is the only thing that marks an order PAID and emails
the owner. `/checkout/success` clears the cart and nothing else; it is an
unauthenticated GET and proves no payment.

**Two things must be set up or no order is ever recorded as paid:**

1. `STRIPE_WEBHOOK_SECRET` in the environment. Without it every delivery is
   rejected with a 400 (deliberately failing closed) and orders stay PENDING.
2. The endpoint registered in the Stripe Dashboard under
   *Developers → Webhooks*, pointing at `https://www.pigscanfly.ca/stripe/webhook`
   and subscribed to `checkout.session.completed`,
   `checkout.session.async_payment_succeeded`,
   `checkout.session.async_payment_failed` and `checkout.session.expired`.
   Copy that endpoint's signing secret into `STRIPE_WEBHOOK_SECRET`.

Checkout enables `adjustable_quantity`, so the customer can change quantities
on Stripe's hosted page after the snapshot is written. The webhook therefore
re-reads the billed line items from Stripe and writes those quantities onto
`OrderItem.quantity`, keeping the cart's original in
`OrderItem.snapshot_quantity` so the change stays auditable. The notification
email is the pick/pack list, so it must not be knowingly stale.

Fulfilment is manual: paid orders show up in the Django admin, and the owner
flips the status to FULFILLED once it ships.

Everything after the payment is recorded is best-effort and cannot cost you the
order — the webhook returns 200 either way, because a non-2xx makes Stripe
retry for three days:

- notification email failed → `notification_error` set, `notified_at` null;
- line items could not be re-read → `reconciliation_error` set, `reconciled_at`
  null, quantities left at the cart's, and the email says loudly that the list
  is unverified.

Note that setting `ADMINS` also switches on Django's built-in error mail, so
unhandled 500s now go to the same address.

## Products / fixtures

Bootstrap products (Holden's books) live in
`main/fixtures/initial_products.yaml`. Rules:

- Fixture rows use **pks 100+** so they never collide with rows created
  directly in prod.
- The primary pod re-runs `manage.py seed_products` on **every deploy**. It
  upserts *fixture-owned* fields (name, description, price, links, tax_code,
  …), so admin edits to those get overwritten — edit the YAML instead.
- Two fields are deliberately **not** fixture-owned and are never touched on
  an existing row: `external_product_id` (generated at runtime) and `stock`
  (managed in the admin). See `SEED_PROTECTED_FIELDS` in
  `main/management/commands/seed_products.py`.
- `external_product_id` (Stripe) is generated lazily on first add-to-cart,
  so seeding needs no Stripe access.

### Stock

`Product.stock` gates whether a physical book can be bought **on this site**.
It defaults to 0, is set by hand in the admin, and does *not* decrement on
purchase.

A book at stock 0 shows "Out of Stock" and its add-to-cart button is
disabled, but the page still lists the Amazon / Bookshop.org / Kindle links
(and the India-specific ones), so the product remains buyable elsewhere —
zero stock means "not from us right now", not a dead page.

Two consequences worth knowing before a deploy:

- **A fresh database seeds every book at stock 0**, so nothing is directly
  purchasable until stock is entered in the admin. That is the intended
  default, not a bug — but it does mean "set the stock" belongs in the
  post-deploy checklist below.
- The Google Merchant feed reports `out_of_stock` for those items, and
  Merchant Center will disapprove them until stock is set.

Stock is re-checked at checkout, not just at add-to-cart: a cart can sit for
days, so a line that sold out in the meantime bounces the customer back to
the cart rather than billing for something that cannot be shipped.

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
(`holdenk/pcfweb:<tag>`) → `kubectl apply` → wait for both rollouts.

### Bump the image tag first

**`deploy.yaml` is the single source of truth for the image tag, and it must
be bumped before every deploy.** `build.sh` reads the tag out of it and
pushes to exactly that tag, so re-using one that is already running is a
silent no-op: `kubectl apply` sees an unchanged Deployment spec, no rollout
is triggered, and the pods keep serving the old image no matter what
`imagePullPolicy: Always` says.

`build.sh` now refuses to start when the tag in `deploy.yaml` matches one the
cluster is already running, and when the `image:` lines in `deploy.yaml`
disagree with each other. There are **three** of them — `web-primary`, the
`wait-for-migrations` initContainer, and `web` — and they all have to match.

### Pre-deploy checklist

1. Bump the tag on all three `image:` lines in `deploy.yaml`.
2. Confirm `pcfweb-secret` carries `SECRET_KEY`, `STRIPE_LIVE_SECRET_KEY` and
   `STRIPE_WEBHOOK_SECRET`. Prod refuses to boot without any of them (see
   *Environment variables*), so a missing one is a failed rollout rather than
   a silent misbehaviour — Kubernetes keeps the old pods serving.
3. Confirm the `STRIPE_SHIPPING_RATES` ids exist under the **live** key.
   Shipping rate ids are livemode-scoped, so a rate created in test mode does
   not exist in live and Stripe rejects the entire session — every physical
   checkout fails. Override the setting per environment rather than editing
   the default.
4. Confirm the Stripe webhook endpoint (`/stripe/webhook`) is registered and
   subscribed to `checkout.session.completed`,
   `checkout.session.async_payment_succeeded`,
   `checkout.session.async_payment_failed` and `checkout.session.expired`.
5. After the rollout: set `stock` in the admin for anything that should be
   directly purchasable (see *Stock*).

**Before running `./build.sh`, check out the image assets as a sibling
directory.** They are deliberately kept out of this repo (`.gitignore`
excludes `main/static/assets/images`), so the build depends on
[`pcfweb-assets`](https://github.com/pigsCanFlyLabs/pcfweb-assets) being
present one level up:

```bash
git clone https://github.com/pigsCanFlyLabs/pcfweb-assets.git ../pcfweb-assets
```

Note that `build.sh` does `rm -rf main/static/assets/images` *before* it
copies the new ones in, so running it without `../pcfweb-assets` present
both fails the build and leaves your local images deleted — re-clone the
sibling repo and re-run to recover.

The Kubernetes objects:

- `pg-bootstrap.yaml` — the database: a [CloudNativePG](https://cloudnative-pg.io/)
  `Cluster` (3 instances, 10Gi `encrypted-local-path` storage, nightly
  backups to B2), plus manual `Backup` and nightly `ScheduledBackup`.
- `deploy.yaml` — the app: `web-primary` (1 replica; runs `migrate` +
  `seed_products` on start), `web` (3 replicas), `web-svc`, and the ingress
  for `www.pigscanfly.ca`.

The app reaches Postgres through the operator-created `pcfweb-pg-rw`
Service; `DBHOST`/`DBNAME`/`DBUSER` are set directly in `deploy.yaml` and
`DBPASSWORD` comes from the `pcfweb-internal-pg-secret` Secret.

### Health checks

All three probes target `/healthz`, which runs a real query and returns 503
when the database is unreachable. They deliberately do **not** target `/`:
the kubelet dials the pod over plain HTTP and sends no `X-Forwarded-Proto`,
so `SecurityMiddleware` answers with a 301 to https before any view or model
code runs — and Kubernetes counts a 3xx as success. Probes against `/` pass
on a completely broken app. `/healthz` is listed in `SECURE_REDIRECT_EXEMPT`
for exactly this reason.

`web`'s pods also run a `wait-for-migrations` initContainer that blocks on
`manage.py migrate --check`. `web-primary` applies the migrations but both
Deployments roll at the same time, so without the gate the serving pods
would run new code against the old schema until the primary caught up.

### Known limitation: uploaded media is ephemeral

`MEDIA_ROOT` is `/opt/app/media` inside the container, with no volume behind
it. Anything uploaded through the admin's `Product.image` field lands on
whichever of the 4 pods served that request, 404s on the other three, and is
gone on restart. Use `image_name` (a file committed to `pcfweb-assets` and
served from `static/`) instead — that is what every fixture product does.
Fixing this properly needs a `ReadWriteMany` volume or object storage, and
the cluster's `encrypted-local-path` StorageClass is `ReadWriteOnce`.

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

### One-time MySQL → Postgres data migration

The site previously ran against an external MySQL. To carry data over:

1. From a checkout of the last pre-Postgres revision (which still has the
   MySQL settings) with access to the old DB:
   `./manage.py dumpdata --natural-foreign -e contenttypes -e auth.Permission -e sessions -e cal_sync_magic -o prod-dump.json`
   (calendar sync no longer ships in this repo, so its rows can't be loaded
   here; drop `-e cal_sync_magic` if that old checkout doesn't have the
   calendar app installed.)
2. On this revision: `kubectl -n pcfweb port-forward svc/pcfweb-pg-rw 5432:5432`,
   set `DBHOST=127.0.0.1` etc., then `./manage.py migrate` and
   `./manage.py loaddata prod-dump.json`.
3. Reset sequences (explicit-pk loads don't advance them):
   `./manage.py sqlsequencereset main auth | kubectl -n pcfweb exec -i pcfweb-pg-1 -- psql -U postgres pigscanfly`
