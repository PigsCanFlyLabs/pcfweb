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

`run_local.sh` needs the sibling `pcfweb-assets` checkout too — see [Image
assets](#image-assets). It re-syncs `main/static/assets/images` from that
checkout on **every** run and reports what it found, warning rather than
refusing so an unrelated missing image cannot stop you getting a server.

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
| `BOOK_ASSET_ROOT` | all | Directory holding the purchased book ZIPs; defaults to `<repo>/book-assets` (`/opt/app/book-assets` in the image). Must **not** be under `STATIC_ROOT` or `MEDIA_ROOT` — see [Digital products](#digital-products). |
| `SITE_BASE_URL` | all | Absolute base for emailed download links. Defaults to `https://www.pigscanfly.ca`. Wrong value = links that go nowhere. |

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
  is unverified;
- a download could not be sent → `digital_delivery_error` set,
  `digital_delivery_sent_at` null, and the owner's email says **NOT DELIVERED**
  so it can be resent by hand.

A **zero-total** order counts as paid. Stripe creates no PaymentIntent for one,
so the session reports `payment_status: "no_payment_required"` and can never
report `"paid"`; the webhook accepts both. This is reachable in practice
because the e-book is pay-what-you-want with a floor of zero.

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

### Physical, digital and service products

`Product.delivery_type` (`PHYSICAL` / `DIGITAL` / `SERVICE`) says how a product
reaches the buyer, and is a separate axis from `Product.mode`, which only says
how Stripe bills. Checkout asks for a shipping address and offers shipping
rates when — and only when — the cart holds something `PHYSICAL`. It used to
infer this from mode and category, which meant a digital-only cart would have
demanded a mailing address and offered the buyer media mail for a PDF.

`Product.is_pwyw` makes `Product.price` a *suggestion*: the Stripe Price is
minted with `custom_unit_amount` and the buyer names their own figure,
including zero. Two constraints come with it, both enforced in code:
`custom_unit_amount` cannot be combined with `adjustable_quantity` on a line
item, and it only works in `payment` mode — so a cart mixing a
pay-what-you-want product with a subscription is refused rather than sent to
Stripe to be rejected.

### Digital products

`Product.sells_ebook` means **we are licensed to distribute this file
ourselves**. It defaults to `False` and the four O'Reilly titles keep it that
way. Fulfilment refuses to send anything unless it is `True`, even when
`delivery_type` is `DIGITAL`, so one mis-set dropdown in the admin cannot email
out a book somebody else holds the rights to.

Delivery is a **signed, expiring link**, not an attachment: a paid order emails
the buyer a `TimestampSigner` token good for 7 days, and `/download/<token>`
streams the ZIP. The files live at `BOOK_ASSET_ROOT` (`/opt/app/book-assets`),
which is deliberately outside both nginx aliases in `conf/nginx.default`
(`/static` and `/media`) — that view is the only way to reach one.

`Product.digital_asset_name` is the filename **stem**; the served file is
`<stem>.zip`. It is admin-editable and therefore untrusted: it is validated
against `^[a-z0-9][a-z0-9_]*[a-z0-9]$`, the extension is appended in code, and
the resolved path is asserted to sit directly in the resolved asset root before
anything is opened.

**Before running `./build.sh` you need a second sibling checkout**, holding the
book archives in Git LFS:

```bash
git clone https://github.com/pigsCanFlyLabs/pcfweb-book-assets.git ../pcfweb-book-assets
cd ../pcfweb-book-assets && git lfs install && git lfs pull
```

`scripts/check-book-assets.sh` runs first and fails the build loudly if any
archive is a Git LFS pointer, is under a megabyte, or lacks ZIP magic bytes.
Without that check a host that never ran `git lfs pull` — or one whose GitHub
LFS quota is exhausted, where clones still succeed and just hand back pointers
— would bake ~130-byte text stubs into the image and email those to customers.

`book-assets/` is in `.gitignore` and **deliberately not** in `.dockerignore`;
adding it there ships an image with no books in it.

That build guard only sees the files it is handed, so it cannot know which
books the catalogue expects. `manage.py check_book_assets` is the other half:
the primary pod runs it on start, right after `seed_products`, and for every
product that is `DIGITAL` with `sells_ebook` set it opens
`<digital_asset_name>.zip` under `BOOK_ASSET_ROOT`. Anything it cannot open is
logged at `ERROR` **and** emailed to `ADMINS`, because the alternative first
notice is a customer who has already paid. It never fails startup (pass
`--fail` if you want the exit code, `--no-email` to keep it to the log), so
one undeliverable book does not stop the site from serving everything else.

### Region-specific buy links

Books carry `amazon_link` and `bookshop_link` (shown to everyone) plus
`amazon_in_link` / `flipkart_link`, which are shown first to visitors whose
IP resolves to India via MaxMind GeoLite2. Country detection needs
`GeoLite2-Country.mmdb` in `GEOIP_PATH` — the Docker build downloads it when
`MAXMIND_LICENSE_KEY` is set (passed as a BuildKit secret so it stays out of
the image history). Without the database the site quietly serves the default
links only.

## Image assets

The product and site images are deliberately kept out of this repo
(`.gitignore` excludes `main/static/assets/images`) and live in
[`pcfweb-assets`](https://github.com/pigsCanFlyLabs/pcfweb-assets), which has
to be checked out one level up:

```bash
git clone https://github.com/pigsCanFlyLabs/pcfweb-assets.git ../pcfweb-assets
cd ../pcfweb-assets
git lfs install
git lfs pull
```

That repo stores its images in Git LFS; without the `git lfs pull` the
checkout holds ~130-byte pointer files instead of images, which copy and serve
without complaint and render as broken images everywhere.

`main/static/assets/images` is therefore a **derived copy**, refreshed by
`scripts/sync-local-assets.sh`. Both `build.sh` and `run_local.sh` call it, so
the deploy path and the local path cannot drift:

| | `build.sh` | `run_local.sh` |
| --- | --- | --- |
| Re-syncs the directory | every run | every run |
| Sibling checkout missing | fails the build | warns, keeps the existing copy |
| LFS pointers instead of images | fails the build | warns |
| Image over 5MB | fails the build | warns |
| Fixture names a file that isn't there | fails the build | warns, lists the pks |

The copy is unconditional and destructive — `rm -rf` then `cp -af`. That is
deliberate, and the reason is worth knowing, because it caused a bug that hid
for weeks:

> `main/static/assets/images` is gitignored, so `git pull` never touches it.
> Before this was shared, only `build.sh` refreshed it. A checkout whose last
> build predated the move of every book cover into `images/book_covers/`
> therefore had a directory that was **full and still wrong** — flat cover
> files, no `book_covers/` subdirectory, and leftovers for retired products.
> Every book cover 404d while the banners and the logo rendered fine, so it
> read as a partial breakage rather than as a missing directory. Because the
> directory existed, a "copy it if it's absent" sync would have fixed
> nothing; the `rm -rf` is the part that does the work.

Running `build.sh` with `../pcfweb-assets` absent no longer deletes your
local copy: the sibling checkout is verified before anything is removed.

## Deploying

`./build.sh` is the whole pipeline: re-sync the sibling `pcfweb-assets` images
and run the four asset guards over them (sibling checkout present, per-file
size ceiling, LFS pointers, fixture references — see
[Image assets](#image-assets)) → validate and stage the
sibling `pcfweb-book-assets` archives → mypy → migration check → tests →
template validation → collectstatic → LFS check over the collected static
tree → multi-arch Docker build/push (`holdenk/pcfweb:<tag>`) →
`kubectl apply` → wait for both rollouts.

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
   a silent misbehaviour — Kubernetes keeps the old pods serving. `build.sh`
   now checks this before it builds, so the failure arrives in a second rather
   than at `kubectl rollout status`. Note the name: an older `pcfweb-secret`
   carries `DJSTRIPE_WEBHOOK_SECRET`, left over from dj-stripe, and that is
   *not* the variable Prod reads. The value is per-endpoint, so take the
   signing secret of the endpoint currently registered at `/stripe/webhook`
   rather than copying the old key across.

   A rollout that got out anyway looks like this: `kubectl get pods` shows two
   generations of `web-primary` (the old one `Running`, the new one
   `CrashLoopBackOff`) and `web` pods stuck in `Init:0/1`, because
   `wait-for-migrations` is waiting on a migration the primary never applied.
   The old pods keep serving throughout, so the site is up and only the deploy
   is stuck.
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

Both `./build.sh` and `./run_local.sh` need the image assets checked out as a
sibling directory — see [Image assets](#image-assets).

The build also needs the book archives in `../pcfweb-book-assets` — see
[Digital products](#digital-products). `build.sh` refuses to build without
them; `run_local.sh` only warns, because locally the sole consequence is that
an e-book download 404s.

The Kubernetes objects:

- `pg-bootstrap.yaml` — the database: a [CloudNativePG](https://cloudnative-pg.io/)
  `Cluster` (3 instances, 10Gi `encrypted-local-path` storage, nightly
  backups to B2), plus manual `Backup` and nightly `ScheduledBackup`.
- `deploy.yaml` — the app: `web-primary` (1 replica; runs `migrate` +
  `seed_products` + `check_book_assets` on start), `web` (3 replicas),
  `web-svc`, and the ingress for `www.pigscanfly.ca`.

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
