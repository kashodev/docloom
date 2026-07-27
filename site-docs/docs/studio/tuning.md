# Tuning & scaffolding

The studio surfaces the throughput knobs that the kernel and `deploy.sh` have, and
can emit a reusable `run.yaml` instead of running.

## Concurrency & tasks

| Flag | catalog step | pdfs step |
|---|---|---|
| `--concurrency` | → `catalogue.concurrency` (LLM calls in flight) | — |
| `--tasks` | → `catalogue.tasks` (>1 = a **sharded**, resumable build) | → `job.tasks` (Cloud Run workers) |
| `--parallelism` | — | → `job.parallelism` (0 ⇒ = tasks) |

`0` means the step's default (catalog `8` / `1`, pdfs `4` / `=tasks`), so nothing
changes unless you set a flag. `tasks > 1` on the catalog step makes the build a
[sharded run](../invoice/catalogue.md) over company ranges, coordinated by the same
[atomic claim](../architecture/concurrency.md) as generation. `tasks` is a Cloud
Run concept, so `local` honours only `--concurrency`.

These are advanced knobs: passed as flags **and** prompted in the wizard
(concurrency + tasks on the catalog step, tasks on pdfs).

## Catalogue re-use

An existing catalogue for a version is reused by default rather than rebuilt —
`--rebuild-catalogue` forces a rebuild. See
[LLM catalogue & secrets → Re-use](catalogue.md#re-use).

## `--scaffold` — write a run.yaml

```bash
docloom studio -p gcp --project <ref> --step pdfs --run-id r1 --total 5000 --scaffold run.yaml
```

Writes the step's config to a **named, commented `run.yaml`** (instead of a
throwaway temp file) that you can drive [`deploy.sh`](../operations/deploy-tool.md)
with directly:

```bash
deploy/gcp/deploy.sh -c run.yaml <command>
```

It reuses the exact config the studio would have run, so `--scaffold` is the bridge
from "let the studio drive it" to "hand-tune the config and drive `deploy.sh`
yourself." gcp only (local has no `run.yaml`).

!!! note "Deferred"
    A **cost preview** before the catalog confirm is a tracked TODO — a naive
    estimate is fragile (prices drift; a reasoning model that ignores `max_tokens`
    breaks it), so the hard `--budget` cap remains the real protection meanwhile.
