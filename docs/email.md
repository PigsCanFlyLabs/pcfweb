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
| `EMAIL_HOST` | ConfigMap (deploy.yaml) | `pigscanfly.ca` |
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

The zone lives in colo-scripts at `playbooks/files/bind-zones/pigscanfly.ca.`
(deployed to the bind hosts by `playbooks/dns-setup.yaml`; note the zone's NS
records point at `dns1`/`dns2.stabletransit.com`, so a change that must be
visible to the public internet has to reach whatever actually serves those —
bump the SOA serial either way or secondaries will not pick it up).

What matters for mail:

- `pigscanfly.ca. MX 200 pigscanfly.ca.` — the relay we submit to is the
  same host that receives the domain's mail, at `71.19.157.174`.
- SPF: `v=spf1 include:_spf.google.com include:sendgrid.net mx ip4:71.19.157.174 ~all`.
  Submitting through `pigscanfly.ca` means the onward hop leaves from an IP
  inside `mx`/`ip4:` — aligned. The `sendgrid.net` include covers what
  already sends `@pigscanfly.ca` mail through SendGrid (Alertmanager, the
  health stack) and pcfweb if it is ever pointed there.
- DKIM: the only published key is the Google-era selector
  `20160214._domainkey`. Mail relayed through `pigscanfly.ca` or SendGrid is
  not DKIM-signed under this domain unless that relay signs it; with SPF
  aligned and no DMARC policy published this delivers, but if a `_dmarc`
  record is ever added, set up the signer first (for SendGrid: domain
  authentication, which adds its `s1`/`s2._domainkey` CNAMEs).

## Switching to SendGrid

The rest of the colo already sends through it (`smtp.sendgrid.net:587`
STARTTLS for Alertmanager, `:465` SMTPS for the health stack; the username
is literally `apikey`). For pcfweb it is four ConfigMap values and one
secret:

1. `EMAIL_HOST: smtp.sendgrid.net`, `EMAIL_PORT: "587"`,
   `EMAIL_USE_TLS: "true"`, `EMAIL_USE_SSL: "false"` (or 465 with the flags
   swapped), `EMAIL_HOST_USER: apikey`.
2. Put the API key in `pcf_email_host_password` in the colo-scripts vault
   and re-run cluster-setup, or edit `pcfweb-secret` in place.
3. Restart the pods. The from addresses can stay `support@pigscanfly.ca`;
   SPF already authorizes SendGrid (above), but complete SendGrid's domain
   authentication before relying on it for DMARC.

## Checking it actually works

From a prod pod (`kubectl -n pcfweb exec -it deploy/web -- bash`):

    cd /opt/app && ./manage.py sendtestemail you@example.com

That exercises host, port, TLS mode, credentials and timeout in one shot and
prints the real SMTP error on failure. After a deploy, the passive signals
are: `Order.notification_error` / `digital_delivery_error` in the admin's
order list (empty on healthy sends), and `check_book_assets` output in the
primary pod's startup log.
