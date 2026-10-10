# 16 — Postgres HA, pooling and backups

Three things live here: the CloudNativePG cluster that provides HA, a review of the connection pooling, and
the backup/restore tooling with the result of an end-to-end drill actually executed against a live database.

Verification status is stated per section. Anything not marked **verified** was not executed — there is no
Kubernetes cluster and no Docker in this environment (see the verification table in the README).

---

## 1. Topology

```
app pods ──> pgbouncer (transaction pooling) ──> rag-pg-rw ──> primary  ─┐
                                                                         ├─ 2 replicas, 1 synchronous
migrations ─> rag-pg-rw (session mode, DIRECT_DATABASE_URL) ─> primary  ─┘
                                                                  └─> WAL archive + nightly base backup (S3)
```

| Connection | Goes through | Env var | Why |
|---|---|---|---|
| Normal app queries | `rag-pg-pooler-rw` (pgbouncer) | `DATABASE_URL` | thousands of short transactions, few server connections |
| Migrations, rebuild advisory lock | `rag-pg-rw` (direct) | `DIRECT_DATABASE_URL` | session-scoped state that transaction pooling breaks |

`app/config.py` refuses to start in `prod` when `DATABASE_URL` looks pooled and `DIRECT_DATABASE_URL` is
unset, because the failure it prevents is silent: an advisory lock taken on a pooled connection is released
at the end of the transaction, so two rebuilds can run at once.

## 2. CloudNativePG cluster — `deploy/k8s/cnpg/cluster.yaml`

**Not verified** (requires a cluster with the operator installed). Reviewed for correctness only.

3 instances, one synchronous commit confirmation (`method: any, number: 1`), pod anti-affinity `required` on
`kubernetes.io/hostname`, WAL on a separate volume, `primaryUpdateMethod: switchover`, and
`enableSuperuserAccess: false`.

Install order matters — the operator CRDs must exist before the cluster:

```bash
kubectl apply --server-side -f https://raw.githubusercontent.com/cloudnative-pg/cloudnative-pg/release-1.27/releases/cnpg-1.27.0.yaml
kubectl -n rag create secret generic rag-pg-app    --from-literal=username=rag --from-literal=password='...'
kubectl -n rag create secret generic rag-pg-backup --from-literal=ACCESS_KEY_ID=... --from-literal=ACCESS_SECRET_KEY=...
kubectl apply -k deploy/k8s/cnpg
```

Two notes worth keeping in mind when editing this file:

* **The `ScheduledBackup` cron has six fields.** CNPG uses `robfig/cron` with seconds first, so
  `"0 30 2 * * *"` is 02:30 daily. A five-field `"30 2 * * *"` parses as *every 30 seconds* of minute 2 —
  the most common CNPG misconfiguration, and it looks right at a glance.
* **Durability vs availability.** With `number: 1` and 3 instances, losing one replica still leaves a
  synchronous candidate; losing both blocks writes rather than losing a committed transaction. Set
  `dataDurability: preferred` if you would rather keep accepting writes.

### Choosing one pooler, not two

`deploy/k8s/base` includes a hand-rolled `pgbouncer` Deployment, and `cluster.yaml` also defines a CNPG
`Pooler`. **Apply only one.** On CNPG, prefer the `Pooler`: it tracks the `-rw` service, so it follows a
failover, which the hand-rolled Deployment does not. To use it, drop `pgbouncer.yaml` from the base in your
overlay and point `DATABASE_URL` at `rag-pg-pooler-rw` instead of `pgbouncer`:

```yaml
# overlays/prod/kustomization.yaml
patches:
  - target: {kind: Deployment, name: pgbouncer}
    patch: '[{"op": "replace", "path": "/spec/replicas", "value": 0}]'   # or omit the resource entirely
```

## 3. Pooling review

Both poolers run `pool_mode = transaction`. That is only safe because `app/container.py` opens its psycopg
pool with `prepare_threshold=0`; with server-side prepared statements on, transaction pooling hands a
statement prepared on one server connection to a client that later lands on another and fails with
`prepared statement "_pg3_0" does not exist`. `tests/test_k8s_manifests.py` asserts that setting still
exists, so the manifests cannot quietly become wrong.

Sizing is consistent across the two definitions: `default_pool_size 25` × 2 pooler replicas = 50 server
connections, against the cluster's `max_connections: 200`, leaving headroom for replicas, the operator and
`superuser_reserved_connections`.

Findings from this review:

| # | Finding | Status |
|---|---|---|
| 1 | `cluster.yaml` documented `kubectl apply -k deploy/k8s/cnpg`, but that directory had no `kustomization.yaml`, so the documented command failed outright. | **fixed** — added, and `tests/test_k8s_manifests.py` now fails if any documented `-k` target lacks one |
| 2 | The hand-rolled pgbouncer had no pod anti-affinity, so both replicas could be scheduled on one node and the `minAvailable: 1` PDB would protect nothing against node loss. | **fixed** — `preferred` anti-affinity on `kubernetes.io/hostname` (not `required`, so single-node dev clusters still schedule both) |
| 3 | The CNPG `Pooler` sets `ignore_startup_parameters`, `query_wait_timeout`, `min_pool_size` and `max_prepared_statements` explicitly; the hand-rolled Deployment sets none of them and relies on the `edoburu/pgbouncer` image's defaults. | **open, unverified** — see below |

### Finding 3, stated honestly

libpq sends `extra_float_digits` at startup, and pgbouncer rejects unknown startup parameters unless they
are in `ignore_startup_parameters`. If the image does not already allow it, every connection through the
hand-rolled Deployment fails with `unsupported startup parameter: extra_float_digits`.

This was **not fixed**, because fixing it blind would be worse than leaving it visible: the correction
depends on which environment variables `edoburu/pgbouncer:v1.23.1-p3` actually reads when it generates
`pgbouncer.ini`, and that could not be checked here (no Docker, and no network access from this session).
Adding an environment variable the image ignores would look like a fix and silently not be one.

On first deployment of the non-CNPG path, run:

```bash
kubectl -n rag exec deploy/pgbouncer -- grep -i 'ignore_startup_parameters\|pool_mode' /etc/pgbouncer/pgbouncer.ini
kubectl -n rag logs deploy/pgbouncer | grep -i 'unsupported startup parameter'
```

If `ignore_startup_parameters` is absent, set `IGNORE_STARTUP_PARAMETERS=extra_float_digits,options,search_path`
on the Deployment (and in `docker-compose.yml`, whose pgbouncer service has the same gap), confirm it landed
in the generated ini with the command above, then update this row. Using the CNPG `Pooler` avoids the
question entirely, which is the recommended path.

## 4. Backup and restore — `scripts/backup_restore.sh`

Logical backups with `pg_dump -Fc` and `pg_restore`, with a sha256 sidecar per dump.

```bash
export DIRECT_DATABASE_URL='postgresql://...'   # session mode, NOT the pooler
export BACKUP_DIR=/var/backups/rag
scripts/backup_restore.sh backup                        # dump + sha256 sidecar, honours BACKUP_KEEP=N
scripts/backup_restore.sh list
scripts/backup_restore.sh verify --restore-test         # restore into a scratch db, then drop it
scripts/backup_restore.sh restore --target-db rag_copy  # restore beside the live database
scripts/backup_restore.sh restore --drop-and-recreate --yes   # destructive, in-place
```

The script reads the real environment and does **not** load `.env` — deliberate for an ops tool, but it
means you must export the URL yourself. It refuses a pooled `DATABASE_URL` unless `ALLOW_POOLER=1`
(`pg_dump` needs a session-mode connection), refuses `--drop-and-recreate` without `--yes`, refuses to
restore over a non-empty database, restores in a single transaction with `--exit-on-error`, and passes
passwords via `PGPASSWORD` so they never reach a command line or the logs.

### Verified: end-to-end drill against the live database

Executed 2026-10-10 against local Postgres 16 at `127.0.0.1:5432/rag` — 1,798 rows across 8 tables,
including 296 LangGraph checkpoints and 1,181 checkpoint blobs from real pipeline runs.

| Step | Command | Result |
|---|---|---|
| 1 | `db_fingerprint.py --json fp_before.json` | 8 tables, 1,798 rows, every table has a primary key |
| 2 | `backup_restore.sh backup` | 333,560 bytes, sha256 sidecar written |
| 3 | `backup_restore.sh verify --restore-test` | 30 TOC entries; restored into a scratch db → 8 tables; scratch dropped |
| 4 | `backup_restore.sh restore --target-db rag_q5_check` | row-for-row identical to step 1 |
| 5 | `backup_restore.sh restore --drop-and-recreate` (no `--yes`) | refused, exit 2 |
| 6 | `backup_restore.sh restore --drop-and-recreate --yes` | **live database dropped** and restored, 30 TOC entries, 8 tables |
| 7 | `db_fingerprint.py --compare fp_before.json` | **MATCHES** — same row count, same key digest, same whole-row digest for all 8 tables |
| 8 | load 3 restored threads through `AsyncPostgresSaver.aget_tuple` | 3/3 deserialized, 18 channels and 8 messages each |

Step 8 is the one that actually answers "did the threads survive". Steps 6–7 prove the bytes came back;
step 8 proves LangGraph can still resume a conversation from them.

Fingerprint of the live data before and after the drop (identical after restore):

| Table | Rows | Key digest | Row digest |
|---|---|---|---|
| `api_keys` | 14 | `ace4ae64035b7998` | `5bb0b0f46b715213` |
| `checkpoint_blobs` | 1181 | `c4e2ec2c7a2bed96` | `0cd695d78ec9f34c` |
| `checkpoint_migrations` | 10 | `b5cea5c1f356480c` | `8c5e9974419faa63` |
| `checkpoint_writes` | 20 | `2d0fb4b6b2c9afd6` | `dbeab4550332cf9c` |
| `checkpoints` | 296 | `4e6763765b2ffe45` | `83c3c51a93f53354` |
| `schema_migrations` | 2 | `68a63609623a10b7` | `ee6e06c8e43521ec` |
| `tenants` | 2 | `a7a25fe534729afb` | `0cd29ddbda80c96a` |
| `thread_activity` | 273 | `fd65603701476197` | `99b117c4ecf98163` |

### `scripts/db_fingerprint.py`

`restore ok: ... (30 TOC entries, 8 tables)` proves the *schema* arrived. It says nothing about the rows, and
a thread is only usable if its checkpoint blobs came back intact — so this tool hashes what matters:

```bash
python scripts/db_fingerprint.py --json before.json      # before a risky operation
python scripts/db_fingerprint.py --compare before.json   # after; exit 1 on any difference
python scripts/db_fingerprint.py --db rag_copy --compare before.json
```

Per table it records the row count, a sha256 over the primary keys, and an md5 over whole rows. Keys are
discovered from the live schema rather than hardcoded, so a new table is covered the day it is added and a
table with no primary key is reported instead of being silently skipped. Rows are hashed in key order, which
makes the digest independent of physical row order — a dump/restore reorders rows freely.

The whole-row digest is not redundant: in the negative control below, `schema_migrations` had an **identical**
key digest and a different row digest, which is exactly the class of damage a key-only check misses.

Output is counts and digests only — row content is hashed, never printed, and the connection string is never
shown, so a fingerprint is safe to paste into an incident ticket.

Negative control (verified): comparing `rag` against the unrelated `q4_obs` database reports a mismatch on 7
of 8 tables and exits 1; comparing `rag` against itself exits 0.

## 5. What is still missing

* The CNPG cluster has never been applied — no failover, switchover or PITR has been exercised.
* `barmanObjectStore` points at `s3://CHANGE-ME-bucket/rag-pg`; restore-from-object-store is untested.
* Finding 3 above (pgbouncer startup parameters on the non-CNPG path).
* `retentionPolicy: 30d` has never been observed expiring a backup.
* The drill covers logical backup/restore of one database. It is not a PITR drill: it recovers to the moment
  of the dump, not to an arbitrary point in time.
