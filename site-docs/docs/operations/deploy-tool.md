# The deploy tool

`deploy/gcp/deploy.sh` runs docsynth on GCP as **Cloud Run Jobs**. It is
config-file driven — one `run.yaml` describes a whole run, and the same script and
config shape run a 1,000-document smoke test and a multi-million-document
production run; only the numbers differ. Every step is **idempotent** (re-running
converges rather than failing on "already exists").

```bash
./deploy.sh -c run.yaml <command> [--set a.b.c=value ...]
```

## Subcommands

| Command | Does |
|---|---|
| `plan` | Show the plan; touch nothing. |
| `provision` | APIs, bucket, Firestore + claim index, service account, IAM. |
| `build` | Build + push the image (Cloud Build). |
| `deploy` | Create/update the Cloud Run job. |
| `run` | Execute — one execution per document slice, blocking (`--wait`). |
| `dispatch` | Execute **detached** — no `--wait`; follow with `status`. |
| `resume` | Re-queue failed units, reclaim crashed ones. |
| `status` | Run progress, from Firestore. |
| `logs` | Recent job logs. |
| `export` | Export golden shards to the configured sink. |
| `catalogue` | Build the LLM content catalogue (one-off). |
| `teardown` | Delete the job(s) and, with `TEARDOWN_BUCKET`, the bucket. |
| `all` | provision + build + deploy + run. |

`dispatch` is what the [studio](../studio/dispatch.md) uses so a cloud run outlives
the terminal. `teardown` honours `ASSUME_YES` / `TEARDOWN_BUCKET` env so the studio
can drive it non-interactively.

Any value can be overridden without editing the file, repeatably:
`--set run.total=1000 --set job.tasks=8`.

## Config surface (`run.yaml`)

| Block | Keys |
|---|---|
| `run.*` | `id`, `total`, `unit_size`, `format`, `catalogue`, `max_line_items`, `llm_usage`, `date_range` (default issue-date window). |
| `documents[]` | Per-slice `count`/`share` + composition axes (locales, companies, archetypes, business_types, conditions, wear, goods_receipt, per-slice `date_range`). |
| `job.*` | `tasks`, `parallelism`, `cpu`, `memory`, `task_timeout`, `max_retries`. |
| `catalogue.*` | `providers`, `fallback`, `budget_usd`, `concurrency`, `tasks`, `unit_size`, `secrets`. |
| `export.sink` | The golden sink URI. |

- **Slices.** `documents[]` splits a run into slices, each sized by `count` or
  `share` (share needs `run.total`). A multi-slice run nests output under
  `runs/<run>/<slice>/…`.
- **Issue-date range.** `date_range: [from, to]` sets the window an invoice's issue
  date is drawn from; every other date binds to it. Omitted ⇒ the default window.
- **`run.catalogue`** wires a published catalogue into generation (`--catalogue`);
  omitted ⇒ the built-in seed catalogue.

The reference config is `deploy/gcp/run.example.yaml`. For the GCP resources and
secrets see [Deploying on GCP](gcp.md); for driving a run see
[Running / resuming / status / export](running.md). The [studio](../studio/overview.md)
synthesizes this same config, so a run is identical whether launched by hand or by
`docsynth studio`.
