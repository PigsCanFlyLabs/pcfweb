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
| `MAILING_LIST_FROM_EMAIL` | all | From address for list mail. Falls back to `DEFAULT_FROM_EMAIL`. |
| `MAILING_LIST_BASE_URL` | all | Used to build confirm/unsubscribe links when there is no request to build them from (CSV import, `send_mailing`). Defaults to `https://www.pigscanfly.ca`. |
| `MAILING_LIST_REDIRECT_HOSTS` | all | Comma-separated extra hostnames an embedded signup form may send the visitor back to via its `next` field. **Adds to** `MAILING_LIST_SITE_DOMAINS` in the settings rather than replacing it, so a new site cannot be added by restating the existing ones and getting one wrong. This allowlist is what stops the CSRF-exempt signup endpoint being an open redirect. |
| `MAILING_LIST_SIGNUP_RATE_LIMIT` | all | Confirmation emails one client address can trigger per hour (default 20; 0 disables). |
| `MAILING_LIST_SEND_BATCH_SIZE` | all | Recipients per click of "send" in the admin (default 100). Must fit inside `GUNICORN_TIMEOUT`. |

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

## Admin

Everything staff-only is under `/timbit/admin/` (Timbit guards the lab — see
the about page). `/admin/` redirects there, including deep links, so old
bookmarks keep working.

`/timbit/admin/home` is the landing page: it lists every admin path,
including the ones the Django admin's own index cannot show because they are
not model changelists (the CSV import, the send page, the embeddable form).

## Mailing list

Subscribers live in `MailingListSubscription`, one row per address per
`InterestArea` — the "area of interest" a subscriber picked. **Anyone who
does not pick one is in the general group**, which a data migration creates
and `InterestArea.get_default()` re-creates if it is ever deleted.

The groups are seeded by migration `0012_seed_interest_areas`:

| Slug | Group | |
| --- | --- | --- |
| `all` | All | **catch-all** — gets every mailing, whichever groups it names |
| `general` | General updates | the default for anyone who does not choose |
| `books` | Books | |
| `dc4k` | Distributed Computing 4 Kids and Executives | |
| `high-performance-spark` | High Performance Spark | |
| `liberatedbread` | Liberated Bread | |
| `fight-health-insurance` | Fight Health Insurance | |

That migration only ever adds, so renaming or deactivating a group in the
admin sticks. **Slugs are interface**, not data: an embedded form on another
site carries one in its markup, so add a new group rather than renaming one.
`sort_order` controls the order they are offered in (editable in the admin);
being listed first is *not* what makes a group the default — the signup form
explicitly pre-selects general, so putting "All" at the top cannot quietly
capture people who did not choose.

`catch_all` is what makes "All" mean all: a mailing addressed to Books
reaches the Books subscribers *and* everyone in a catch-all group, once
each. Without it, picking "All" would opt someone into a group named after
everything and then leave them out of every mailing addressed to a topic.

An address is not on the list until it confirms:

1. `POST /mailing-list/subscribe` records a PENDING row and mails a
   confirmation link.
2. `GET /mailing-list/confirm/<token>` marks it SUBSCRIBED. Only SUBSCRIBED
   rows are ever mailed anything else. Only a PENDING row confirms:
   unsubscribing does not rotate the token, so an old or forwarded
   confirmation link cannot undo somebody leaving the list — signing up again
   is the way back, and that mails a fresh link.
3. `GET /mailing-list/unsubscribe/<token>` asks, `POST` to the same URL does
   it. Every mailing carries that link plus a `List-Unsubscribe` header, so
   mail clients show a real unsubscribe button.

### The signup endpoint is CSRF exempt

That is deliberate: the point is that a plain `<form>` pasted onto *another*
site can post to it, and such a form has no token to send. It is safe because
nothing there reads the session or acts for a logged-in user — there is no
authority for a forged request to borrow, and the worst it can do is create a
PENDING row that does nothing until that address clicks the link. The
protections that matter instead:

- double opt-in, so an address cannot be added by whoever posted it;
- a honeypot field (`website`), answered as a success so bots learn nothing;
- `MAILING_LIST_SIGNUP_RATE_LIMIT` confirmations per client address per hour;
- `next=` is only honoured for hosts in `MAILING_LIST_REDIRECT_HOSTS` (plus
  `ALLOWED_HOSTS`), so it cannot be turned into an open redirect;
- re-posting an already-subscribed address is a no-op, so nobody can knock a
  subscriber back to PENDING.

The unsubscribe endpoint is exempt too, because RFC 8058 one-click
unsubscribe means the mail client posts it directly, from no origin.

### Putting the form on another site

`/mailing-list/embed` shows copy-and-paste markup for each group, and
`main/static/mailing-list/signup-form.html` is the same form as a static file
with the options commented. Two options:

- **a plain form** posting to `https://www.pigscanfly.ca/mailing-list/subscribe`
  with a hidden `interest` naming the group. Nothing loads from us. Add a
  hidden `next` (https) to bounce the visitor back to that site;
- **an iframe** of `/mailing-list/embed/<slug>`, which keeps the visitor on
  the other site with no redirect setup. That page is
  `xframe_options_exempt`; nothing else on the site is.

Our own sites are set up for the `next` redirect already, apex and `www.`
both — `MAILING_LIST_SITE_DOMAINS` in the settings:

| Site | Group to use |
| --- | --- |
| `liberatedbread.com` | `liberatedbread` |
| `distributedcomputing4kids.com` | `dc4k` |
| `distributedcomputing4executives.com` | `dc4k` |
| `highperformancespark.com` | `high-performance-spark` |

Anywhere else, a `next` is ignored and the visitor gets our thank-you page
instead; add the host to `MAILING_LIST_REDIRECT_HOSTS` (or to the settings
list) to change that. `/mailing-list/embed` shows the current list.

### Importing and sending

- `/timbit/admin/mailing-list/import` uploads a CSV. It reads a header row
  (Mailchimp and Substack exports work as they come) or a bare column of
  addresses, defaults to importing rows as already-subscribed — an import is
  you asserting you have consent, so it sends no confirmations — and refuses
  to re-add anyone who unsubscribed. Do a dry run first; it reports per-row
  errors without writing anything.
- `/timbit/admin/mailing-list/send/<id>` sends a mailing to everyone or to
  the groups it names. Send yourself a test first. Sending goes out
  `MAILING_LIST_SEND_BATCH_SIZE` at a time, claiming a `MailingListDelivery`
  row *before* each mail goes out, so **nobody is mailed twice**: a reload, a
  worker timeout, a second click or a concurrent send continues where it
  stopped. The claim is unique on (message, address), not just on the
  subscription row, so someone in two of the selected groups gets one copy
  even if two senders overlap. The first batch also freezes the audience —
  somebody who subscribes mid-send is not added to a mailing that predates
  them, and a finished mailing does not quietly reopen. `./manage.py
  send_mailing <id> --send` does the same from a shell for a list too long to
  click through.
- `./manage.py import_newsletter_subscribers --apply` moves confirmed
  django-newsletter subscribers over, one interest area per newsletter. The
  site's own forms no longer post to django-newsletter; run this once so the
  addresses collected before this feature are not stranded.

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
6. First rollout with the mailing list: the admin moved to `/timbit/admin/`
   (old links redirect). The interest groups are seeded by migration, so
   check the list and deactivate anything you do not want offered, set
   `MAILING_LIST_REDIRECT_HOSTS` for any site whose embedded form should
   bounce visitors back to itself, and run
   `./manage.py import_newsletter_subscribers --apply` once so the addresses
   collected through django-newsletter are on the new list. Send yourself a
   test from the send page before sending anything to anybody else.

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
