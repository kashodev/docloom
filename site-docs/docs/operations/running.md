# Running / resuming / status / export

The same lifecycle applies whether you drive it with the [`docsynth` CLI](../core/overview.md)
locally, [`deploy.sh`](deploy-tool.md) on GCP, or the [studio](../studio/overview.md).

## Run

- **Local:** `docsynth generate --run-id r1 --total 1000 [--catalogue <uri>] [--format pdf|html]`.
- **Cloud (blocking):** `deploy.sh -c run.yaml run` — one execution per slice, waits.
- **Cloud (detached):** `deploy.sh -c run.yaml dispatch` — returns immediately; a
  multi-hour run outlives the terminal. This is the studio's default.

Concurrency is just running the worker again — another process, another task —
against the same `--state`; the [atomic claim](../architecture/concurrency.md)
splits the units. `job.tasks` is how many tasks a Cloud Run execution starts.

## Follow

- `docsynth status --run-id r1 --state <uri>` — a snapshot; `--wait` streams until
  the run is terminal.
- `docsynth studio status --project <ref> --run r1 [--wait]` — reattaches to a
  dispatched cloud run from any machine that has the project registered, reading
  the run's state store (no live job to hold). See [Studio → Dispatch & status](../studio/dispatch.md).
- `deploy.sh status` / `deploy.sh logs`.

## Resume

A run that leaves **failed units exits non-zero**, so a `--wait`, a Cloud Run
execution, or a CI step reads a broken run as broken. Recover with:

- `docsynth generate … --resume` (local) or `deploy.sh resume` (cloud).

Resume re-queues failed units and reclaims units abandoned by crashed workers
(expired leases), then continues. It is safe to run repeatedly — generation is
idempotent (a re-generated index overwrites identical content).

## Pause / cancel

`docsynth pause` / `docsynth cancel` set the run state so workers stop claiming new
units; `resume` clears a pause.

## Export

Once a run is complete, load its golden shards into a queryable sink:

- `docsynth export --run-id r1 --sink <uri> [--storage-prefix …]`
- `deploy.sh export` (uses `export.sink` from the config).
- Studio: the **export** step.

Export is re-runnable and independent of generation — see
[Invoice → Export](../invoice/export.md).
