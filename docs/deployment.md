# Deployment & configuration

How to run docloom on one laptop or a few hundred cloud workers, and exactly
which URIs to point at on each platform. The *why* behind the model — units,
the atomic claim, leases — is in [concurrency.md](concurrency.md); this is the
wiring.

## The only two rules

Everything on this page follows from two constraints:

1. **Every worker must reach the same StateStore.** That is where the atomic
   claim lives, and it is the only coordination between workers. A store that
   one worker can't reach is a worker that can't participate.
2. **`sqlite://` is a single-box store.** It coordinates processes and threads on
   one machine via a file lock. It does *not* coordinate machines. The moment you
   want more than one instance, the state store has to be networked
   (`firestore://`, `dynamodb://`).

Storage and the golden sink have no such constraint — they are plain reads and
writes, so any worker with credentials can use them.

## Configuration surface

Three URIs decide the whole deployment. They are CLI flags (`docloom generate`),
and the calling code never changes when they do:

| Concern | Flag | Local default | Cloud options |
|---|---|---|---|
| Blob storage | `--storage` | `file://./out` | `gs://bucket/prefix` · `s3://bucket/prefix` |
| Run state | `--state` | `sqlite://./runs.db` | `firestore://project/database` · `dynamodb://table` |
| Golden sink | (export) | `parquet://` · `duckdb://` | `bigquery://project/dataset?staging=gs://…` |

Plus the two knobs that shape the work itself:

- `--total` — how many documents the run produces.
- `--unit-size` — documents per work unit. One unit is simultaneously the claim
  granularity, one golden shard, and one export read. Smaller units = finer
  parallelism and more shards; larger = less overhead. 1000 is a sane default.

**Rule of thumb:** aim for at least a few units per worker, so a slow unit can't
leave a worker idle at the tail. 200 workers on a 1M-document run wants
`--unit-size` around 1000 (1000 units, 5 per worker), not 100 000 (10 units).

### What is actually implemented

Be aware of the gaps before planning a platform:

| Adapter | Status |
|---|---|
| `file://`, `sqlite://`, `parquet://`, `duckdb://` | ✅ built, tested |
| `gs://` | ✅ built, verified against fake-gcs-server |
| `firestore://` | ✅ built, verified against the Firestore emulator |
| `dynamodb://` | ✅ built, verified against DynamoDB Local |
| `s3://` | ✅ built, unit-tested (no emulator run yet) |
| `bigquery://` | ✅ built; end-to-end test needs a real project |
| `az://` (Azure Blob) | ❌ **not written** |
| Cosmos DB / Table Storage state | ❌ **not written** |
| Athena / Synapse sinks | ❌ **not written** |

---

## Local — one box, no cloud account

The default. Nothing to provision.

```bash
pip install docloom
playwright install chromium

docloom generate --run-id run_2026_07 --total 50000 --unit-size 1000
# → file://./out, sqlite://./runs.db
```

Concurrency comes from running the same command several times against the same
`runs.db` — the SQLite claim keeps them off each other's units:

```bash
for i in $(seq 1 8); do
  docloom generate --run-id run_2026_07 --total 50000 &
done
wait
```

Size it by cores: Chromium rendering is the bottleneck, so roughly one worker per
core, and remember each worker holds its own browser process.

Export and evaluate locally with DuckDB — the same SQL that runs on BigQuery:

```bash
docloom export --run-id run_2026_07 --sink duckdb://./golden.db
```

**Resume** after an interruption re-queues failed units and reclaims any unit a
crashed worker abandoned:

```bash
docloom generate --run-id run_2026_07 --total 50000 --resume
```

---

## GCP — the reference stack

**Compute: Cloud Run Jobs.** The natural fit: a job has a task count, each task
is an identical container, and tasks are independent — exactly the drain-loop
model. Set `--tasks` to the worker count; every task runs the same command and
pulls units until the pool is empty.

```bash
gcloud run jobs create docloom-run \
  --image gcr.io/PROJECT/docloom:latest \
  --tasks 100 --parallelism 100 \
  --task-timeout 3600 \
  --set-env-vars RUN_ID=run_2026_07 \
  --command docloom \
  --args generate,--run-id=run_2026_07,--total=1000000,--unit-size=1000,\
--storage=gs://my-bucket/runs,--state=firestore://my-project/(default)
```

- **Storage:** `gs://bucket/prefix`
- **State:** `firestore://project/database` — the networked claim, via document
  transactions.
- **Sink:** `bigquery://project/dataset?staging=gs://bucket/staging` — external
  tables over the staged Parquet, so `decimal128` lands as `NUMERIC` and the
  cent-exact join survives.

Grant the job's service account: object read/write on the bucket, Firestore user,
and BigQuery data editor + job user.

**Cloud Run Service instead of a Job** only if you want an always-on pool driven
by requests; for a bounded generation run, Jobs are simpler and cheaper.

**Scaling:** raise `--tasks`. The claim is the only shared point, and Firestore
handles the contention — losers retry and take the next unit.

---

## AWS

Two good options, same URIs.

**AWS Batch (array jobs)** — best for a large bounded run. An array job of size N
launches N identical containers; each drains units.

```bash
aws batch submit-job \
  --job-name docloom-run --job-queue docloom-queue \
  --job-definition docloom:1 \
  --array-properties size=100 \
  --container-overrides 'command=["docloom","generate","--run-id","run_2026_07",
    "--total","1000000","--unit-size","1000",
    "--storage","s3://my-bucket/runs","--state","dynamodb://docloom-state"]'
```

**ECS/Fargate** — set the service/task `desired-count` to the worker count.
**Bare EC2** — run the container N times per host; same command.

- **Storage:** `s3://bucket/prefix`
- **State:** `dynamodb://table` — the claim is a conditional write
  (`IF state = 'pending'`), so exactly one racing worker wins a unit. Create the
  table with partition key `pk` (string) and sort key `sk` (string), on-demand
  billing; no GSI needed. Add `?region=` / `?endpoint_url=` when the default
  credential chain isn't enough.
- **Sink:** no Athena adapter yet. Today: export Parquet to `s3://`, then either
  point Athena at it manually or load into BigQuery. `duckdb://` over a synced
  local copy works for smaller runs.

> **Spot / preemptible instances.** AWS Batch on Spot and Fargate Spot can kill a
> worker mid-unit. That is handled: every claim carries a **lease**, and a unit
> whose lease lapses is returned to the pool. But DynamoDB (like Firestore)
> reclaims *explicitly*, not on every claim — so on Spot, either run
> `--resume` periodically, or run a small sweeper that calls
> `reclaim_expired_units`. Without one, an abandoned unit waits for the next
> resume. Tune the lease (`lease_seconds`, default 900s) above your worst-case
> unit time, or a slow-but-alive worker's unit can be stolen.

**Do not** point multiple AWS instances at `sqlite://` on EFS. File locking over
network filesystems is exactly where SQLite's guarantees stop holding.

---

## Azure — partially supported

The compute story is fine; **the adapters are not written yet**. Plan
accordingly.

- **Compute:** Azure Container Apps Jobs is the Cloud Run Jobs equivalent
  (parallel task count, one container image). Azure Batch is the AWS Batch
  equivalent for large array-style runs.
- **Storage:** Blob Storage would be an `az://` adapter — **not implemented**.
  Interim: mount blob storage and use `file://`, or write against `s3://` via a
  compatible gateway.
- **State:** Cosmos DB (conditional writes via ETag `if-match`) or Table Storage
  would slot behind `StateStore` the same way DynamoDB does — **not
  implemented**. Interim: reach a `firestore://` or `dynamodb://` store across
  clouds if latency allows, or stay single-instance with `sqlite://`.
- **Sink:** Synapse/Fabric over the staged Parquet, or DuckDB over blob —
  **no adapter**; export Parquet and query it externally.

Each missing piece is a small adapter behind an existing protocol — one class,
no pipeline change. See the `dynamodb://` store for the shape to copy.

---

## Choosing a platform

| You want | Use |
|---|---|
| To try it, or a run that fits one machine | Local: `sqlite://` + `file://` + `duckdb://` |
| A large run, GCP shop | Cloud Run Jobs + `gs://` + `firestore://` + `bigquery://` |
| A large run, AWS shop | Batch array job + `s3://` + `dynamodb://` |
| Cheap compute, tolerant of eviction | Spot/preemptible + a networked store + a reclaim sweeper |
| Azure | Container Apps Jobs, but expect to write the storage/state adapters |

## Operating a run

```bash
docloom status --run-id run_2026_07 --state <uri>   # progress by unit state
docloom pause  --run-id run_2026_07 --state <uri>   # workers drain and stop
docloom cancel --run-id run_2026_07 --state <uri>
docloom generate --run-id run_2026_07 --resume …    # re-queue failed + reclaim
```

Pause and cancel need no signal to the workers: run state gates the claim, so
workers simply stop being handed units. Resume flips it back, returns failed
units to the pool, and reclaims anything a crashed worker left behind.
