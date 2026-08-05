# August 2026 — pcfweb-pg backup migration to the barman-cloud plugin: runbook

pcfweb-pg had **zero working backups** from its creation until this
migration. State observed 2026-07-30 (during the fighthealthinsurance backup
incident, whose runbook diagnosed this cluster as its Phase 4 —
`fighthealthinsurance/docs/pg-backup-reconciliation-runbook-2026-07.md`):

| Symptom | Evidence |
|---|---|
| `pcfweb-pg` stuck `walArchivingFailing` since creation (Jul 11) | `kubectl -n pcfweb describe backup pcfweb-pg-backup-manual` (the first manual backup already hit it) |
| Every `pcfweb-pg-nightly-*` Backup forever `pending`; none minted at all after `20260718000000` | `kubectl -n pcfweb get backup` |
| No WAL archive, no restorable base backup — **no recovery path at all** | `pg_stat_archiver` showed `archived_count=0` |

## What happened

1. The cluster was born (Jul 11) with the **in-tree**
   `spec.backup.barmanObjectStore` — deprecated since CNPG 1.26 and
   **removed in 1.28+**. The installed operator is 1.30, and the PG18
   operand images no longer ship the barman-cloud binaries, so the archive
   command failed on the very first WAL segment and every backup request
   sat `pending` forever. Nothing alerted: `monitoring.enablePodMonitor`
   was `false` and no PrometheusRule existed for this namespace.
2. Independently, from Jul 18 the CNPG operator was wedged cluster-wide
   (duplicate barman-cloud plugin installs + a graveyard of pending Backups
   for a dead cluster starving the scheduling controller) — that is why the
   doomed nightlies stopped even being *created* on exactly Jul 18. That
   half was repaired on the fighthealthinsurance side; their runbook is the
   authority on it.
3. The fix, committed here: `pg-bootstrap.yaml` migrated to the
   **barman-cloud plugin** (an `ObjectStore` CR + `spec.plugins` with
   `isWALArchiver: true`, same bucket and same `pcfweb-pg` prefix), the
   nightly recreated with `method: plugin` at 09:00 UTC, WAL bounded with
   `max_slot_wal_keep_size`, PodMonitor enabled, `pg-alerts.yaml` paging on
   the silent failure modes, and `scripts/check-pg-backup-health.sh` as the
   one-shot verifier. `build.sh` re-applies the manifests on every deploy,
   so the schedule and rules self-heal.

## Prerequisites

The cluster-side plugin repair must already hold — exactly ONE
plugin-barman-cloud install (the pinned helm release from colo-scripts
`playbooks/cluster-setup.yaml`), healthy and holding its leader lease.
Checks 1–3 of `./scripts/check-pg-backup-health.sh` verify precisely this;
run it first and fix the plugin (per the fighthealthinsurance runbook)
before touching pcfweb.

## Phase 0 — capture state (read-only)

```bash
kubectl -n pcfweb get cluster pcfweb-pg -o jsonpath='{.status.phase}: {.status.phaseReason}{"\n"}'
kubectl -n pcfweb get scheduledbackup,backup
kubectl -n pcfweb exec pcfweb-pg-1 -c postgres -- psql -U postgres -xc \
  "SELECT archived_count, failed_count, last_archived_time, last_failed_wal FROM pg_stat_archiver;"
# WAL piling up / disk at risk? (10Gi volume; segments are 16MB)
for p in pcfweb-pg-1 pcfweb-pg-2 pcfweb-pg-3; do
  kubectl -n pcfweb exec "$p" -c postgres -- sh -c \
    'echo "$HOSTNAME: $(ls /var/lib/postgresql/data/pgdata/pg_wal | grep -Ec "^[0-9A-F]{24}$") segments"; df -h /var/lib/postgresql/data | tail -1'
done
```

Unarchived WAL is exempt from `max_slot_wal_keep_size`, so a long-dead
archiver means pg_wal grows without bound — segments in the hundreds
(>4GiB) or the volume ≥70% means finish this runbook **now**. **Never
hand-delete anything in `pg_wal`.**

## Phase 1 — delete the doomed backup objects

Zero-risk: deleting Backup/ScheduledBackup CRs never touches bucket data,
and every existing Backup here is unrestorable by construction.

```bash
# The old nightly's Backups carry backupOwnerReference: self, so deleting
# the schedule garbage-collects its stuck pending CRs with it:
kubectl -n pcfweb delete scheduledbackup pcfweb-pg-nightly
# The one-shot bootstrap Backup is not owned by anything; delete explicitly
# (it is also gone from pg-bootstrap.yaml — the recreated nightly's
# immediate: true replaces its prove-it-now role):
kubectl -n pcfweb delete backup pcfweb-pg-backup-manual
kubectl -n pcfweb get backup    # expect: No resources found
```

Deleting (not patching) the ScheduledBackup matters for Phase 3:
`immediate: true` only fires when the object is **created**, so the apply
must create it fresh for the migration to prove itself immediately.

## Phase 2 — verify the bucket's `wals/` prefix is empty

On its first archive the plugin checks that the `wals/` prefix under the
serverName is EMPTY; any leftover object makes it fail forever with
`Expected empty archive` (fhi-pg-main-9 lost 24 days to exactly this —
stale WAL from an earlier incarnation under the same serverName). The
in-tree path here never archived anything, so the prefix *should* be empty
— but a pre-Jul-11 pcfweb-pg incarnation could have written there. Check:

```bash
B2="--endpoint-url https://s3.us-west-004.backblazeb2.com"
aws $B2 s3 ls --recursive --summarize s3://pcfweb-pg-backup/pcfweb-pg/wals/ | tail -5
# ONLY if non-empty — park it, don't destroy it (and never "fix" this by
# deleting the .check-empty-wal-archive marker in PGDATA; the check stops
# two clusters from interleaving WAL in one archive):
aws $B2 s3 mv --recursive s3://pcfweb-pg-backup/pcfweb-pg/wals/ \
  s3://pcfweb-pg-backup/graveyard/pcfweb-pg-stale-wals/
```

`base/` does not block archiving, but if it holds objects predating this
migration they are not restorable — park them the same way to keep the
listing honest.

## Phase 3 — apply and roll

```bash
kubectl apply -f pg-bootstrap.yaml     # ObjectStore + migrated Cluster + fresh nightly
kubectl apply -f pg-alerts.yaml
```

(Equivalently, a normal `./build.sh` deploy applies both — but it also
builds and rolls the app, so for the DB-only migration the two applies are
the surgical version.)

The Cluster edit (in-tree `backup:` → `plugins:`) changes the instance pod
spec (the plugin sidecar is injected), so the operator rolls the instances
itself — replicas first, then a switchover; expect one brief primary
switch:

```bash
kubectl -n pcfweb get cluster pcfweb-pg -w    # -> "Cluster in healthy state"
kubectl -n pcfweb get pods -w                 # instances restart one at a time
```

## Phase 4 — prove it end-to-end

```bash
# Archiver: force a segment switch on the CURRENT primary and watch it ship.
PRIMARY=$(kubectl -n pcfweb get cluster pcfweb-pg -o jsonpath='{.status.currentPrimary}')
kubectl -n pcfweb exec "$PRIMARY" -c postgres -- psql -U postgres -c 'SELECT pg_switch_wal();'
sleep 30
kubectl -n pcfweb exec "$PRIMARY" -c postgres -- psql -U postgres -xc \
  "SELECT archived_count, failed_count, last_archived_time FROM pg_stat_archiver;"
```

Read it like this: `archived_count` rising from 0 with a fresh
`last_archived_time` is the win. `failed_count` is a **lifetime** counter
holding the whole broken era — it never resets on success; only its recency
matters. If archiving is not working, the real error is in the sidecar:

```bash
kubectl -n pcfweb logs "$PRIMARY" -c plugin-barman-cloud --since=15m | tail -30
```

(`Expected empty archive` → Phase 2. S3 403s → the `pg-backup` Secret.
Checksum errors → the ObjectStore's B2 env vars did not reach this pod;
roll the instance.)

Then the base backup — the freshly created nightly has `immediate: true`,
so one is already running:

```bash
kubectl -n pcfweb get backup -l cnpg.io/cluster=pcfweb-pg \
  --sort-by=.metadata.creationTimestamp -w      # -> completed
aws $B2 s3 ls s3://pcfweb-pg-backup/pcfweb-pg/base/ | tail
kubectl -n pcfweb get prometheusrule pcfweb-pg-backup-wal   # alerts registered
./scripts/check-pg-backup-health.sh             # -> ALL CHECKS PASSED
```

## Phase 5 — restore drill (before trusting any of it)

A completed base backup plus a live WAL archive is still theory until a
restore has succeeded once. Boot a throwaway cluster in a scratch namespace
that bootstraps `recovery` from `pcfweb-backup-store` (fhi's
`pg-copy.yaml` / `pg-recover.yaml` are the reference shape, adapted to the
plugin's `externalClusters` form), check the app schema and row counts look
sane, then delete it. Two rules: the drill cluster must NOT have
`isWALArchiver` pointed at the same serverName (it would interleave WAL
into the real archive), and the drill teaches you the real RTO — write it
down here when done.

## Keeping it fixed

- `build.sh` re-applies `pg-bootstrap.yaml` and `pg-alerts.yaml` on every
  deploy: a hand-suspended nightly (`suspend: false` is explicit in the
  manifest) or an edited rule set self-heals. Neither apply can mint a
  second plugin install — colo-scripts owns the plugin as a single pinned
  helm release, and nothing here touches it.
- `./scripts/check-pg-backup-health.sh` is the one-shot probe after any
  operator/plugin change and whenever backups feel doubtful; the
  always-on version is `pg-alerts.yaml` (archiver failing/stalled, newest
  backup >26h, WAL past 2/4GiB on the 10Gi volume, replica loss) paging
  through the cluster's Alertmanager email.
- Never `kubectl apply` the plugin's upstream release `manifest.yaml`;
  helm-via-playbook is the only installer (two installs deadlock on the
  leader lease — the July 2026 outage).
