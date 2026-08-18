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
| `DJANGO_SUPERUSER_USERNAME` / `DJANGO_SUPERUSER_PASSWORD` / `DJANGO_SUPERUSER_EMAIL` | primary pod (optional) | The admin account: `ensure_admin_account` creates it on a fresh database and converges it (rotated password, drifted flags) on every primary boot. Both core variables unset = clean skip; one without the other = loud failure. The names are the ones Django's own `createsuperuser --noinput` reads. See [Admin](#admin). |
| `MAXMIND_LICENSE_KEY` | build.sh (image build) | Bundles the GeoLite2 country DB for region-specific buy links; optional. |
| `GEOIP_PATH` | all | Directory holding `GeoLite2-Country.mmdb`; defaults to `<repo>/geoip` (set to `/opt/app/geoip` in the image). |
| `BOOK_ASSET_ROOT` | all | Directory holding the purchased book ZIPs; defaults to `<repo>/book-assets` (`/opt/app/book-assets` in the image). Must **not** be under `STATIC_ROOT` or `MEDIA_ROOT` — see [Digital products](#digital-products). |
| `SITE_BASE_URL` | all | Absolute base for emailed links — download links, and mailing-list unsubscribe links built without a request. Also the `Site` domain the seed migration creates when there is none. Defaults to `https://www.pigscanfly.ca`. Wrong value = links that go nowhere. |
| `MAILING_LIST_FROM_EMAIL` | all | From address for list mail. Falls back to `DEFAULT_FROM_EMAIL`. |
| `MAILING_LIST_SIGNUP_RATE_LIMIT` | all | Confirmation emails one client address can trigger per hour (default 20; 0 disables). |
| `MAILING_LIST_SEND_BATCH_SIZE` | all | Recipients per click of "send" in the admin (default 100). Must fit inside `GUNICORN_TIMEOUT`. |
|  |  | *All four `MAILING_LIST_*` integers go through `parse_int`, which falls back to the default on a blank or unparseable value. A `ValueError` at settings import happens on every pod before anything can log it, and the deploy gate reports that as "the database is unreachable".* |
| `MAILING_LIST_IMPORT_NOTICE_MAX` | all | Most freshly imported addresses the import page will email the "list changed" notice to (default 500). Above it, the import still happens and the notice is declined. |

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

## Admin

Everything staff-only is under `/timbit/admin/` (Timbit guards the lab — see
the about page). `/admin/` redirects there, including deep links, so old
bookmarks keep working.

`/timbit/admin/home` is the landing page: it lists every admin path,
including the ones the Django admin's own index cannot show because they are
not model changelists (the CSV import, the send page, the embeddable form).

The account that logs in there is provisioned at deploy time, not by hand:
`manage.py ensure_admin_account` runs on every primary boot (see
`scripts/start-server.sh`) against the `DJANGO_SUPERUSER_USERNAME` /
`DJANGO_SUPERUSER_PASSWORD` / `DJANGO_SUPERUSER_EMAIL` variables — in the
cluster these live in `pcfweb-secret`, provisioned out of the colo-scripts
vault (`playbooks/cluster-setup.yaml`, vault keys `PCF_ADMIN_USER` /
`PCF_ADMIN_PASSWORD` / `PCF_ADMIN_EMAIL`). On a fresh database it creates the
superuser; after that it converges, so rotating the password is an edit to
the vault plus a re-run of that playbook and a rollout of `web-primary`.
Unset variables mean a clean skip (local dev never needs them). Two edges are
deliberate: an existing **non**-superuser with the configured username is
never promoted (`auth.User` is also the customer table), and a deactivated
superuser stays deactivated — a lockout in the admin survives deploys.

## Mailing list

**The subscribers are django-newsletter's.** One `Newsletter` is one interest
area, and its `Subscription` rows are the addresses — along with the double
opt-in email, the confirm and unsubscribe pages under `/newsletter/`, and the
CSV/vCard import in its admin. None of that is reimplemented here.

Three things are ours, because that app does not do them:

1. `POST /mailing-list/subscribe` — a **CSRF-exempt** signup endpoint, so a
   plain `<form>` pasted onto another site can post to it.
2. `MailingListMessage` — sending one mailing across *several* lists without
   anybody getting two copies. django-newsletter submits a message to one
   newsletter at a time and does not dedupe between them.
3. The import page and `SuppressedAddress`. django-newsletter has an import
   page, but it cannot check a suppression list, cannot tell the people
   involved that anything happened, and reads CSV through `unicodecsv` — an
   sdist-only package last released in 2017, which is not worth adding to the
   production image to do what the standard library does.

Plus a `pre_save` signal (`main/mailing.py`) that replaces a subscription's
activation code when it is unsubscribed. django-newsletter reuses one code for
both actions and does not change it, so the original "confirm your
subscription" email otherwise keeps a working link — a forwarded copy, or a
mail scanner reaching it late, could put somebody back on a list they left.

### The lists

Seeded by migration `0014_seed_interest_areas`:

`all` · `general` · `books` · `dc4k` · `high-performance-spark` ·
`liberatedbread` · `fight-health-insurance`

**Anyone who does not pick one lands on `general`**, which the migration
creates and `mailing.default_newsletter()` re-creates if it is ever deleted.
The migration only ever adds, so renaming or hiding a list in the admin
sticks. **Slugs are interface**, not data: an embedded form on another site
carries one in its markup, so add a new list rather than renaming one.

`all` is for people who want everything, and it means it: **a mailing
addressed to any _public_ list also reaches everyone on `all`**, including a
list added years after they subscribed. Without that, creating a new interest
would silently stop reaching the people who asked for all of them. Anyone on
both gets one copy, and `MailingListMessage.audience_description()` — shown on
the send page and in the admin's mailing list — says "and everyone on All" so
it is never a surprise.

*Public* is the limit, and it is what makes the internal test list usable: a
**hidden** list (`visible = False`) is not part of "everything", so a mailing
addressed only to hidden lists does not pull `all` in. Losing that is what
would turn the test list below into a trap.

**Signing up for a topic does not put anybody on `all`** — that would
contradict what the subscribe page promises them — but every signup form
carries a *ticked-by-default* "send me all updates" checkbox (`all_updates`).
Ticked, it subscribes them to `all` **instead of** the topic: `all` already
receives every mailing addressed to any public list, so one row covers both and
the confirmation email names exactly the list being confirmed.

That last part is load-bearing. An earlier version created a second row
carrying the first one's activation code, and because the code was copied
server-side from whatever row already existed, anyone who knew an address could
post it with the box ticked and have *that person's own* confirmation click put
them on `all`. A confirmation click can only honestly confirm what the email it
came from says, so nothing links two rows any more.

To reach every confirmed subscriber, `all` or not, send a mailing with **no
lists selected**.

Being listed first is *not* what makes a list the default — the signup form
pre-selects `general` explicitly.

Each list must be attached to a `Site` or django-newsletter's own confirm and
unsubscribe pages 404 on it; the seed migration does that, creating the
`Site` row itself if `contrib.sites` has not got round to it yet.

### The signup endpoint is CSRF exempt

That is deliberate: the point is that a form on *another* site can post to it,
and such a form has no token to send. It is safe because nothing there reads
the session or acts for a logged-in user — there is no authority for a forged
request to borrow, and the worst it can do is create an unconfirmed
subscription that does nothing until that address clicks the link. The
protections that matter instead:

- double opt-in, so an address cannot be added by whoever posted it;
- a honeypot field (`website`), answered as a success so bots learn nothing;
- two ceilings, checked *before* anything is written so a flood cannot fill the
  table either: `MAILING_LIST_SIGNUP_RATE_LIMIT` per source per hour, and
  `MailingListSubscribeView.PER_ADDRESS_LIMIT` (3) per **target address**. The
  second is the one that matters — `X-Forwarded-For` is client-supplied and
  nginx appends to it, so a limit keyed only on the source is bypassed by
  varying it, and an unparseable value shares one bucket rather than escaping
  the count. The target address is the one thing a caller cannot vary while
  still achieving anything;
- **the suppression list**, so a bounced or complained-about address is not
  even sent a confirmation;
- **one answer for every submission.** New, already subscribed, suppressed,
  honeypotted, rate-limited: all indistinguishable from outside. Saying which
  would be a membership oracle on an open, CORS-wildcarded endpoint, and these
  lists include things people would rather not have confirmed about them;
- the `name` field is whitespace-collapsed and bounded, because it is rendered
  into the confirmation email and anyone can post any address here;
- it never redirects anywhere a submission asked it to, so it cannot be turned
  into an open redirect;
- re-posting an already-subscribed address is a no-op, so nobody can knock a
  subscriber back to unconfirmed.

### Putting the form on another site

`main/static/mailing-list/signup-form.html` is the whole mechanism: a plain
`<form>` posting to `https://www.pigscanfly.ca/mailing-list/subscribe`, with
the options commented in the file. Paste it into any page on any site. Nothing
loads from us, so it cannot slow that page down or break when we redeploy, and
**nothing is inferred from which site the form is on** — the list is an
explicit `interest` field, so there is no per-domain configuration to fall out
of step with somebody else's markup. Set it to one of the slugs above; the
admin's Interest areas page lists them.

After submitting, the visitor lands on a thank-you page here. There is
deliberately no redirect-back mechanism: doing that safely needs an allowlist
of hosts, which is exactly the per-site configuration this avoids.

### Importing and sending

- **Importing** is `/timbit/admin/mailing-list/import`, which does two jobs
  from one upload because Mailchimp gives you two files:

  - *Subscribers* — pick **which list** they go on (there is deliberately no
    default; importing into the wrong list is the mistake to be afraid of) and
    they are added as subscribed, with no confirmation email, because an
    import is you asserting you already have consent. Leave **"email everyone
    imported to say the list changed"** ticked and each of them gets a short
    notice saying which list they are on with an unsubscribe link — that is
    the escape hatch that makes skipping the confirmation defensible. Above
    `MAILING_LIST_IMPORT_NOTICE_MAX` addresses it imports but declines to send
    those, and tells you to write a mailing instead (that path batches; this
    one is a loop inside one request).
  - *Addresses to suppress* — Mailchimp's unsubscribed and cleaned exports go
    here. Every address in the file lands on the **suppression list** and is
    taken off every list it is currently on.

  A CSV, a Mailchimp export or a Google Forms one, as it comes: any heading
  containing "email" is the address and any containing "name" is the name,
  everything else is ignored, and a row that makes no sense is skipped rather
  than failing the upload. A bare column of addresses works too. Addresses
  already on the chosen list are left alone — **including anyone who
  unsubscribed from it**, so a stale export cannot resurrect them.

- **The suppression list** (`SuppressedAddress`, at
  `/timbit/admin/main/suppressedaddress/`) is the never-email list: bounces,
  complaints, anyone who asked to be left alone. Every import is checked
  against it and nothing on it is ever added. It is separate from the
  unsubscribed flag on a subscription, which only means "not this list" —
  this means "not at all". Add one address at a time in the admin, or a
  fileful from the import page.

  It is checked on the public signup too, and at send time. Not just because
  of who ends up on a list: *mailing* a suppressed address at all is what gets
  a domain blocked, and the signup endpoint is open to the internet, so
  otherwise anyone could have us mail a complained-about address on demand.
  Somebody suppressed who genuinely wants back has to be taken off the list
  first.
- **Sending** is `/timbit/admin/mailing-list/send/<id>`, linked from both the
  mailing list in the admin and each mailing's own change page, and from
  `/timbit/admin/home`: write a mailing, pick the lists (or none for
  everyone), send yourself a test, then send.

  For a *real* end-to-end check — real batch, real delivery row, real SMTP —
  load the test list and send to that:

  ```bash
  ./manage.py loaddata mailing_list_test_group
  ```

  That creates a hidden "Test (internal only)" list whose only subscriber is
  `holden@pigscanfly.ca`, so a mailing pointed at it cannot reach anybody else.
  `visible: false` is doing two jobs there: keeping the list off the public
  signup form, **and** keeping `all` out of its audience (see above). Both are
  tested, the second with a subscriber on `all` present — otherwise that test
  could never fail. It goes
  out `MAILING_LIST_SEND_BATCH_SIZE` at a time, claiming a
  `MailingListDelivery` row *before* each mail, so **nobody is mailed twice**:
  a reload, a worker timeout, a second click or a concurrent send continues
  where it stopped. The claim is unique on (message, address), not just on the
  subscription row, so somebody on two of the selected lists gets one copy
  even if two senders overlap. The first batch also freezes the audience —
  somebody who subscribes mid-send is not added to a mailing that predates
  them, and a finished mailing does not quietly reopen.

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

### Format groups (one work, several SKUs)

"Distributed Computing 4 Kids (and Executives)" is one book sold three ways —
paperback (pk 104), Executive Edition (105) and e-book (106) — each with its
own ISBN, price, tax code and product page. A `ProductGroup` says those rows
are one work, so:

- `/products` and the homepage carousel show **one card** for the group,
  titled with the group's name, priced at the shown member's price, and
  carrying an "Available in N formats: …" line;
- each product page renders a **format chooser** listing every format with its
  own price, linking to that format's own page.

To group SKUs, add a `main.productgroup` row to the fixture (pks 200+, a
separate sequence from products) and give each member `group`, `format_label`
and `format_order`:

```yaml
- model: main.productgroup
  pk: 200
  fields:
    name: "Distributed Computing 4 Kids (and Executives)"
```

```yaml
    group: 200
    format_label: "Paperback"
    format_order: 0
```

`format_order` is lowest-first and also decides **which member the collapsed
card links to** — the format to show somebody who has not chosen yet. If that
member is filtered out of a listing (delisted, or a category page), the group
still appears, represented by a surviving member.

Grouping is presentation only. It never merges SKUs: identifiers, prices,
stock, Stripe products, product pages and Google Merchant feed entries all
stay per-row, and no chooser entry is an add-to-cart — each one links to the
page that sells that format. Pinned end to end by
`main/tests/test_product_groups.py`.

Groups are seeded before products (a `group` key is a foreign key and lands in
the same INSERT as the rest of the row), so a product may name a group
declared anywhere in the file. A product naming a group that does not exist
fails the seed with a message rather than a foreign-key error.

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
6. First rollout with the mailing list: the admin moved to `/timbit/admin/`
   (old links redirect). The lists are seeded by migration, so check them and
   hide anything you do not want offered — existing django-newsletter
   subscribers are already on their own lists and need no migration. Check the
   `Site` row's domain, since django-newsletter builds its confirmation links
   from it. Send yourself a test from the send page before sending anything to
   anybody else.

Both `./build.sh` and `./run_local.sh` need the image assets checked out as a
sibling directory — see [Image assets](#image-assets).

The build also needs the book archives in `../pcfweb-book-assets` — see
[Digital products](#digital-products). `build.sh` refuses to build without
them; `run_local.sh` only warns, because locally the sole consequence is that
an e-book download 404s.

The Kubernetes objects:

- `pg-bootstrap.yaml` — the database: a [CloudNativePG](https://cloudnative-pg.io/)
  `Cluster` (3 instances, 10Gi `encrypted-local-path` storage), the
  barman-cloud plugin `ObjectStore` it backs up through (base backups +
  continuous WAL archiving to B2, 30d retention), and the nightly
  `ScheduledBackup` (09:00 UTC, `method: plugin`). The in-tree
  `barmanObjectStore` config this cluster started on was removed from CNPG
  1.28+ and never archived a byte — `docs/pg-backup-migration-2026-08.md`
  has the incident story and the live-migration runbook.
- `pg-alerts.yaml` — `PrometheusRule` paging on the failure modes that were
  silent last time: WAL archiver failing or stalled, newest backup >26h
  old, pg_wal growth on the 10Gi volume, replica loss.
- `deploy.yaml` — the app: `web-primary` (1 replica; runs `migrate` +
  `ensure_admin_account` + `seed_products` + `check_book_assets` on start),
  `web` (3 replicas), `web-svc`, and the ingress for `www.pigscanfly.ca`.

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

### Database backups

Nightly base backup at 09:00 UTC plus continuous WAL archiving to the
`pcfweb-pg-backup` B2 bucket via the barman-cloud CNPG plugin, 30-day
retention — so point-in-time recovery, not just a daily snapshot.
`build.sh` re-applies `pg-bootstrap.yaml` and `pg-alerts.yaml` on every
deploy, so the schedule (and its `suspend: false`) and the alert rules
self-heal.

`./scripts/check-pg-backup-health.sh` is the one-shot verifier: plugin
install healthy, archiver advancing, WAL bounded, newest backup <26h,
schedule present and matching the manifest. Run it after any
operator/plugin change and whenever backups feel doubtful; the always-on
version is `pg-alerts.yaml` through the cluster's Alertmanager. History and
the migration runbook: `docs/pg-backup-migration-2026-08.md`.

### Known limitation: uploaded media is ephemeral

`MEDIA_ROOT` is `/opt/app/media` inside the container, with no volume behind
it. Anything uploaded through the admin's `Product.image` field lands on
whichever of the 4 pods served that request, 404s on the other three, and is
gone on restart. Use `image_name` (a file committed to `pcfweb-assets` and
served from `static/`) instead — that is what every fixture product does.
Fixing this properly needs a `ReadWriteMany` volume or object storage, and
the cluster's `encrypted-local-path` StorageClass is `ReadWriteOnce`.

#### Thumbnails used to be caught by this too

Using `image_name` did **not** avoid the above, and for a long time nobody
noticed. The source cover was indeed served from `static/`, but the *thumbnail*
of it was not: `easy-thumbnails` resolves its output storage through the
`easy_thumbnails` entry in `STORAGES` and, finding none configured, fell back
to `default_storage` — `MEDIA_ROOT`. So every book cover on `/` and `/products`
rendered as `/media/…290x380_q85.jpg`, generated lazily by whichever pod
happened to render the page and existing only there.

The page render and the browser's follow-up request for the image are
load-balanced independently, so pod A wrote the file and emitted the URL while
pods B and C answered that URL with a 404 — and Cloudflare cached the 404 for
four hours (`Cache-Control: max-age=14400`). That is what turned a per-request
race into an outage that looked stable and per-book ("every cover except
Learning Spark") rather than flaky, and why it was never reproducible under
`run_local.sh`: one process, one filesystem, no CDN, so the process that writes
the thumbnail is the one that serves it.

The fix treats a thumbnail of a *static* source as what it is — a build
artifact, fully determined by a file already baked into the image. The
`easy_thumbnails` storage alias now points at `STATIC_ROOT`, and
`manage.py pregenerate_thumbnails` (run by `scripts/checks.sh` after
`collectstatic`) materialises every one of them into the tree the Dockerfile
`COPY`s, so all replicas serve byte-identical files off the read-only image
layer and nothing is generated at request time. `build.sh` then runs
`manage.py pregenerate_thumbnails --check`, which is read-only on purpose: a
check that generates what it cannot find can never fail.

Generation needs no database. It goes through `generate_thumbnail()` plus an
explicit storage save rather than `get_thumbnail()`, because the latter also
writes `easy_thumbnails` cache rows — rows that land in whatever database the
*build host* has (in CI, none at all) and that production never reads, since
both storages are local and `thumbnail_exists()` compares file mtimes.

#### Running the checks without `pcfweb-assets`

CI checks out this repository alone, so it has no sibling `pcfweb-assets` and
therefore no book covers. `scripts/checks.sh` passes
`--allow-absent-asset-tree` for that case. It is strictly all-or-nothing: it
skips the covers only when **not one** of them is present, prints a banner
saying exactly how many went unverified, and still fails on a tree that exists
with anything wrong in it — a missing cover, a stale thumbnail, an
unmaterialised LFS pointer. `build.sh` never passes the flag; that unconditional
`--check` is the seal on the image that actually ships.

Deliberately not solved by having CI clone `pcfweb-assets`: `images/` is ~47MB
across 91 Git LFS objects, so an `lfs: true` checkout would pull that on every
run of every PR and exhaust the free 1 GiB/month LFS bandwidth in a few weeks —
after which CI fails on a quota error that has nothing to do with the change
under test.

Uploaded-image thumbnails now land in `STATIC_ROOT` as well. That is not a fix
for them — the upload itself is still ephemeral and pod-local, per the section
above — it just moves the thumbnail alongside it.

### One-time cluster prerequisites (not in this repo)

1. CloudNativePG operator installed cluster-wide.
2. The barman-cloud CNPG plugin (`plugin-barman-cloud`), installed in
   `cnpg-system` as the **single** pinned helm release that colo-scripts
   `playbooks/cluster-setup.yaml` manages — it provides the
   `barmancloud.cnpg.io` CRDs and the backup/WAL sidecars. Never install
   it a second way; duplicate installs deadlock on the plugin's leader
   lease (see `docs/pg-backup-migration-2026-08.md`).
3. The kube-prometheus-stack (colo-scripts `playbooks/monitoring.yaml`),
   which scrapes the cluster's PodMonitor and evaluates `pg-alerts.yaml`.
4. The `encrypted-local-path` StorageClass.
5. Secrets in the `pcfweb` namespace:
   - `pcfweb-superuser-pg-secret` — `kubernetes.io/basic-auth`,
     username `postgres` + password.
   - `pcfweb-internal-pg-secret` — `kubernetes.io/basic-auth`,
     username `pigscanfly` + password (the app role).
   - `pg-backup` — `PG_ACCESS_KEY_ID` / `PG_ACCESS_SECRET_KEY` for the
     `pcfweb-pg-backup` B2 bucket (use a bucket dedicated to pcfweb).
   - `pcfweb-secret` — the app env (SECRET_KEY, Stripe, email, …), plus the
     optional `DJANGO_SUPERUSER_*` trio for the admin account (see
     [Admin](#admin)). Provisioned by colo-scripts
     `playbooks/cluster-setup.yaml`.

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
