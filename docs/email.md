# Outbound email: every sender, every setting, and the DNS it depends on

This is the record of how mail leaves pcfweb and what has to be true for it to
arrive. It exists because the failure mode is silence: every caller in this
codebase deliberately catches a failed send and records it instead of raising,
so a misconfigured relay does not break a single page — it just quietly stops
order notifications, download links and error reports.

Verified 2026-07-28.

## What sends mail

All of it goes through Django's SMTP backend as configured on `Prod` in
`pigscanfly/settings.py`. `Dev` writes to `sent_emails/` on disk and never
touches the network.

| Sender | Trigger | From | To | Failure lands in |
|---|---|---|---|---|
| `Order.notify_owner()` | Stripe webhook marks an order paid (or the success page fulfils it first) | `DEFAULT_FROM_EMAIL` | `ADMINS` (`ORDER_NOTIFICATION_EMAIL`) | `Order.notification_error`, pod log |
| `Order._deliver_digital_goods()` | Same fulfilment, orders with digital items | `DEFAULT_FROM_EMAIL` | the customer, plus a copy to `SALES_COPY_EMAILS` | `Order.digital_delivery_error`, pod log |
| `Order.send_receipt()` | Same fulfilment, every paid order | `DEFAULT_FROM_EMAIL` | the customer, plus a copy to `SALES_COPY_EMAILS` | `Order.receipt_error`, pod log |
| `PurchaseFeedback.notify_owner()` | A buyer answers "what made you buy this?" on the checkout success page | `DEFAULT_FROM_EMAIL` | `ADMINS` (`ORDER_NOTIFICATION_EMAIL`) | pod log only — the answer itself is a row in the admin, so a failed send loses only the notification |
| `manage.py check_book_assets` | Primary pod startup | `DEFAULT_FROM_EMAIL` | `ADMINS` | pod log only |
| Django's `AdminEmailHandler` | Any 500 while `DEBUG=False` | `SERVER_EMAIL` | `ADMINS` | pod log only |
| Password reset (`django.contrib.auth`) | User asks on `/accounts/` | `DEFAULT_FROM_EMAIL` | the user | the request 500s |
| django-newsletter | Manual submission from the admin | per-newsletter sender rows | subscribers | admin output |

The three fulfilment senders and the startup audit run where a hang cannot
be afforded — which is why `EMAIL_TIMEOUT` exists and is 10 seconds. Note
that "the webhook" is not the only caller: `CheckoutSuccessView` runs the
same `fulfil_order` inline (`main/views.py`) when the webhook has not landed
yet, so those three can also send in a request the buyer is watching render.

## Copies of the sale emails

The two customer-facing sale emails are copied to `SALES_COPY_EMAILS`. The
owner otherwise only ever sees the order notification, which is a pick/pack
list rather than the message that reached the buyer — so "the link in my
email is broken" could not be answered without asking them to forward it.

**The copy is its own message, not a Bcc on the buyer's**, and that is the
load-bearing detail. `smtplib.sendmail` raises `SMTPRecipientsRefused` only
when *every* envelope recipient is refused, so a copy address sharing the
buyer's envelope turns "the buyer's address was rejected" into a silent
success — and `send_receipt()` / `_deliver_digital_goods()` stamp
`receipt_sent_at` / `digital_delivery_sent_at` on anything that does not
raise, which the fulfilment guards then never retry. A customer would have
paid, received nothing, and the admin row would say delivered. Keeping the
buyer alone on their envelope is what preserves "refused means recorded".

The same separation is why nothing configured here can cost a sale: Django's
SMTP backend runs every recipient through `sanitize_address` *before* the
send and outside its own `except smtplib.SMTPException`, so a malformed
address raises `ValueError` out of whichever message carries it. On the
copy's own message that costs the copy; on the buyer's it would have cost
the receipt. `sales_copy_recipients()` also validates each entry and drops
unusable ones with a warning, and the copy send itself never raises — a
failed copy must not have the order recorded as unsent, or the next
redelivery would mail the customer a second receipt.

The buyer's own address is dropped from the copy list (compared on the
mailbox, so a display-name entry still matches), so the owner buying from
their own shop does not get their receipt twice.

The copy carries an `X-PCF-Copy: sale` header and is otherwise byte-identical
to what the customer got, subject included. That header is what an inbox rule
should filter on to file these away, and it is what lets the test suite keep
saying "exactly one receipt went out" without counting the copy.

Deliberately not applied to the mailing list, which sends one message per
subscriber: a copy rule there would mean a copy of every mailing times every
recipient. `MailingListMessage.send_test()` previews one instead, and the
admin's send page is the record of what went out.

## Where each setting comes from

Precedence in the pod is: `pcfweb-db-config` ConfigMap over `pcfweb-secret`
Secret (deploy.yaml lists the ConfigMap second in `envFrom`) over the code
defaults in `settings.py`. The ConfigMap and the code defaults are pinned to
each other by `DeployManifestTest.test_the_mail_relay_in_the_manifest_matches_the_code_defaults`.

| Variable | Set in | Value today |
|---|---|---|
| `EMAIL_HOST` | ConfigMap (deploy.yaml) | `mail.pigscanfly.ca` — the MX name, never the bare apex (see DNS below) |
| `EMAIL_PORT` | ConfigMap | `465`, the submissions port (RFC 8314) — not 25, the relay port, which networks block outbound and servers refuse AUTH on |
| `EMAIL_USE_TLS` | ConfigMap | `false` (no STARTTLS; 465 is TLS from the first byte) |
| `EMAIL_USE_SSL` | ConfigMap | `true` — both flags on refuses to boot |
| `EMAIL_HOST_USER` | ConfigMap | `support` |
| `EMAIL_HOST_PASSWORD` | `pcfweb-secret`, from the colo-scripts vault (`pcf_email_host_password` in `passwd.yml`, applied by `playbooks/cluster-setup.yaml`) | that mailbox's password; empty disables SMTP AUTH |
| `EMAIL_TIMEOUT` | code default | `10` seconds |
| `DEFAULT_FROM_EMAIL` | code default | `support@pigscanfly.ca` |
| `SERVER_EMAIL` | code default | follows `DEFAULT_FROM_EMAIL`. Django's own default is `root@localhost`, which relays reject — never leave this to the framework |
| `ORDER_NOTIFICATION_EMAIL` | code default | `support@pigscanfly.ca`; feeds `ADMINS` |
| `SALES_COPY_EMAILS` | code default | `holden@pigscanfly.ca`; comma-separated, empty sends no copies |

Changing the relay is therefore a ConfigMap edit plus a pod restart. A
rebuild is only needed if the *defaults* should change.

## The DNS this rides on

The zone is served by Cloudflare (`woz`/`iris.ns.cloudflare.com`). The bind
zone files in colo-scripts (`playbooks/files/bind-zones/`) are retired
copies — nothing deploys from them anymore, and they have drifted from the
live records; make DNS changes in Cloudflare and treat `dig` as the source
of truth, not that directory. Live records as queried 2026-07-28:

- `pigscanfly.ca MX 200 mail.pigscanfly.ca.`, and `mail.pigscanfly.ca` is
  `71.19.157.174` / `2605:2700:0:3:a800:ff:fef5:4975` — the machine the
  apex pointed at before Cloudflare.
- The apex and `www` resolve to Cloudflare edge IPs (`104.21.x`,
  `172.67.x`). **This is why `EMAIL_HOST` names the MX host and must never
  be the bare domain**: Cloudflare's proxy does not carry SMTP, so
  `pigscanfly.ca:25` reaches an edge with nothing listening and every send
  burns its full timeout before failing.
- SPF: `v=spf1 include:_spf.google.com mx ip4:71.19.157.174 ~all`.
  Submitting through `mail.pigscanfly.ca` means the onward hop leaves from
  an IP the `mx` and `ip4:` mechanisms both cover — aligned, no DNS change
  needed for pcfweb's sending. (Note this record does *not* list SendGrid,
  which Alertmanager and the health stack in the colo send through — their
  `@pigscanfly.ca` mail soft-fails SPF. That is their problem, not
  pcfweb's, but it is fixed in Cloudflare if anyone gets to it.)
- DKIM: the only published key is the Google-era selector
  `20160214._domainkey`. Mail relayed through `mail.pigscanfly.ca` is not
  DKIM-signed under this domain unless that server signs it; with SPF
  aligned and no DMARC policy published this delivers, but set up a signer
  before ever adding a `_dmarc` record.

## If the relay ever changes

The knobs are all in the ConfigMap (host, port, one of the TLS flags, user)
plus the password in `pcfweb-secret`, so it is an edit and a pod restart.
Two things to keep true: the new relay's network must be in the live SPF
TXT record (in Cloudflare) before mail rides it, and the From addresses can
stay `support@pigscanfly.ca` either way. For reference, the colo's other
senders use SendGrid (`smtp.sendgrid.net:587` STARTTLS, username literally
`apikey`, an API key as the password) — but pcfweb's chosen path is the
domain's own server above.

## Checking it actually works

From a prod pod (`kubectl -n pcfweb exec -it deploy/web -- bash`):

    cd /opt/app && ./manage.py sendtestemail you@example.com

That exercises host, port, TLS mode, credentials and timeout in one shot and
prints the real SMTP error on failure — the two things DNS cannot tell you:
whether the server actually listens on 465 (a refused or timed-out
connection here means it does not — flip the ConfigMap to `587` with
`EMAIL_USE_TLS` on and `EMAIL_USE_SSL` off), and whether it presents a
certificate valid for `mail.pigscanfly.ca`. After a deploy, the passive
signals are: `Order.notification_error` / `receipt_error` /
`digital_delivery_error` in the
admin's order list (empty on healthy sends), and `check_book_assets` output
in the primary pod's startup log.

For the DNS side of the story, ask the live zone, never the retired copies
in colo-scripts:

    dig +short MX pigscanfly.ca
    dig +short TXT pigscanfly.ca
    dig +short A mail.pigscanfly.ca
