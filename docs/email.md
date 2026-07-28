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
| `Order.notify_owner()` | Stripe webhook marks an order paid | `DEFAULT_FROM_EMAIL` | `ADMINS` (`ORDER_NOTIFICATION_EMAIL`) | `Order.notification_error`, pod log |
| `Order._deliver_digital_goods()` | Same webhook, orders with digital items | `DEFAULT_FROM_EMAIL` | the customer | `Order.digital_delivery_error`, pod log |
| `manage.py check_book_assets` | Primary pod startup | `DEFAULT_FROM_EMAIL` | `ADMINS` | pod log only |
| Django's `AdminEmailHandler` | Any 500 while `DEBUG=False` | `SERVER_EMAIL` | `ADMINS` | pod log only |
| Password reset (`django.contrib.auth`) | User asks on `/accounts/` | `DEFAULT_FROM_EMAIL` | the user | the request 500s |
| django-newsletter | Manual submission from the admin | per-newsletter sender rows | subscribers | admin output |

The first four run where a hang cannot be afforded — inside the webhook or
during startup — which is why `EMAIL_TIMEOUT` exists and is 10 seconds.

## Where each setting comes from

Precedence in the pod is: `pcfweb-db-config` ConfigMap over `pcfweb-secret`
Secret (deploy.yaml lists the ConfigMap second in `envFrom`) over the code
defaults in `settings.py`. The ConfigMap and the code defaults are pinned to
each other by `DeployManifestTest.test_the_mail_relay_in_the_manifest_matches_the_code_defaults`.

| Variable | Set in | Value today |
|---|---|---|
| `EMAIL_HOST` | ConfigMap (deploy.yaml) | `mail.pigscanfly.ca` — the MX name, never the bare apex (see DNS below) |
| `EMAIL_PORT` | ConfigMap | `25` |
| `EMAIL_USE_TLS` | ConfigMap | `true` (STARTTLS) |
| `EMAIL_USE_SSL` | ConfigMap | `false` — both on refuses to boot |
| `EMAIL_HOST_USER` | ConfigMap | `support` |
| `EMAIL_HOST_PASSWORD` | `pcfweb-secret`, from the colo-scripts vault (`pcf_email_host_password` in `passwd.yml`, applied by `playbooks/cluster-setup.yaml`) | that mailbox's password; empty disables SMTP AUTH |
| `EMAIL_TIMEOUT` | code default | `10` seconds |
| `DEFAULT_FROM_EMAIL` | code default | `support@pigscanfly.ca` |
| `SERVER_EMAIL` | code default | follows `DEFAULT_FROM_EMAIL`. Django's own default is `root@localhost`, which relays reject — never leave this to the framework |
| `ORDER_NOTIFICATION_EMAIL` | code default | `support@pigscanfly.ca`; feeds `ADMINS` |

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
prints the real SMTP error on failure — including a STARTTLS certificate
mismatch, the one thing DNS cannot tell you (the server has to present a
certificate valid for `mail.pigscanfly.ca`). After a deploy, the passive
signals are: `Order.notification_error` / `digital_delivery_error` in the
admin's order list (empty on healthy sends), and `check_book_assets` output
in the primary pod's startup log.

For the DNS side of the story, ask the live zone, never the retired copies
in colo-scripts:

    dig +short MX pigscanfly.ca
    dig +short TXT pigscanfly.ca
    dig +short A mail.pigscanfly.ca
