# Running docloom on GCP — a 25k test run

A concrete walkthrough of the reference stack: Cloud Run Jobs for compute, GCS
for documents and golden shards, Firestore for run state. Everything here is
`gcloud`; the script that does it is
[`deploy/gcp/deploy.sh`](../deploy/gcp/deploy.sh).

The general model — which platform maps to what, and why — is in
[deployment.md](deployment.md). This is the specific, runnable version.

**Target for this document**

| | |
|---|---|
| Project | `crawler-rag-data-2026` (existing) |
| Documents | 25,000 invoices, PDF |
| Compute | Cloud Run Job, **4 tasks in parallel** |
| Storage | new GCS bucket `crawler-rag-data-2026-docloom` |
| Run state | new Firestore `(default)` database, Native mode |
| Estimated cost | **~$1–2** for the run, pennies/month to keep the output |

---

## 0. Where do the LLM API keys go?

**For this run: nowhere. You do not need one.**

The invoice pack is `PROCEDURAL` — it declares
`ContentCapability(ContentMode.PROCEDURAL)`, generating every description,
company and figure from its built-in seed catalogue with no model involved. A
25k invoice run makes **zero LLM calls**, so there is no key to leak, no secret
to rotate, and no spend to cap. That is the point of the procedural path.

Even for an LLM-backed pack (contracts), *document generation* still makes no
calls — it reads a catalogue built earlier. Keys are needed only for that
**offline catalogue build**.

### When you do need one: Secret Manager, never the image or the job definition

```bash
# 1. Create the secret (paste the key, then Ctrl-D)
gcloud secrets create anthropic-api-key --replication-policy=automatic \
  --project=crawler-rag-data-2026
gcloud secrets versions add anthropic-api-key --data-file=- \
  --project=crawler-rag-data-2026

# 2. Grant the job's service account access to *this secret only*
gcloud secrets add-iam-policy-binding anthropic-api-key \
  --member="serviceAccount:docloom-run@crawler-rag-data-2026.iam.gserviceaccount.com" \
  --role=roles/secretmanager.secretAccessor \
  --project=crawler-rag-data-2026

# 3. Reference it from the job — resolved at task start, never stored in the job
gcloud run jobs deploy docloom-catalogue \
  --set-secrets=ANTHROPIC_API_KEY=anthropic-api-key:latest \
  ...
```

`ANTHROPIC_API_KEY` is what the Anthropic SDK reads by default, so
`AnthropicProvider()` picks it up with no wiring.

**What not to do, and why:**

| Don't | Because |
|---|---|
| Bake it into the image | Anyone with pull access to Artifact Registry has your key, and it persists in every layer and tag forever |
| `--set-env-vars=ANTHROPIC_API_KEY=sk-...` | Plaintext in the job definition — visible to anyone with `run.jobs.get`, and in `gcloud run jobs describe` output and deploy logs |
| Commit a `.env` | The repo gitignores `.env`, and that guard should never be tested |
| A project-wide `secretAccessor` role | Bind on the individual secret; the job needs one key, not all of them |

**Rotation:** add a new version (`gcloud secrets versions add`). Pinning
`:latest` means the next execution picks it up — no redeploy. Pin an explicit
version number instead if you want changes to be deliberate.

---

## 1. Prerequisites

```bash
gcloud auth login
gcloud config set project crawler-rag-data-2026
gcloud --version   # 470+ recommended; `run jobs deploy` needs a recent one
```

You need `roles/owner`, or the combination of Cloud Run admin, Storage admin,
Datastore owner, Artifact Registry admin, Cloud Build editor and Service Account
admin.

---

## 2. Provision

```bash
cd deploy/gcp
./deploy.sh provision
```

Every step is idempotent — re-run it freely. It:

1. **Enables APIs** — Run, Artifact Registry, Cloud Build, Firestore, Storage,
   Secret Manager.
2. **Creates an Artifact Registry repo** (`docloom`, Docker format) in
   `us-central1`.
3. **Creates the bucket** `gs://crawler-rag-data-2026-docloom`, regional,
   `STANDARD`, with **uniform bucket-level access** — IAM only, no per-object
   ACLs, which is both simpler to reason about and all the job needs.
4. **Creates the Firestore `(default)` database** in `nam5`, Native mode.
5. **Creates the service account** `docloom-run` and grants it the minimum:

   | Role | Scope | Why |
   |---|---|---|
   | `roles/storage.objectAdmin` | **the bucket only** | write documents and shards |
   | `roles/datastore.user` | project | Firestore has no per-database IAM |

### Why `(default)` and Native mode

Firestore's **free tier applies to the `(default)` database** — 1 GiB stored,
50k reads, 20k writes and 20k deletes per day. A named second database gets no
free quota, so "cheapest" means `(default)`. Native mode (not Datastore mode) is
what the client library and its transactions expect.

**This run fits inside the free tier comfortably.** With `--unit-size 500`,
25,000 documents is 50 units, and Firestore sees roughly:

```
  1 run document + 50 unit documents        =  51 writes   (planning)
  50 claims + 50 completions                = 100 writes   (the work)
  ------------------------------------------------------
  ~150 writes total, against 20,000/day free
```

Run state is deliberately tiny: units carry index *ranges*, never document
content. The 25,000 documents themselves go to GCS, which is billed by the byte
and costs cents.

---

## 3. Build the image

```bash
./deploy.sh build
```

Uses **Cloud Build**, not local Docker. Two reasons: no local daemon required,
and it builds `linux/amd64` — which a laptop on Apple silicon would otherwise
get wrong, producing an image Cloud Run cannot start.

The [`Dockerfile`](../Dockerfile) is based on
`mcr.microsoft.com/playwright/python`, so Chromium and its fonts are pinned and
matched to Playwright. That pinning is what makes rendering reproducible across
machines; a mismatched Chromium changes glyph rasterisation and page breaks.

---

## 4. Deploy the job

```bash
./deploy.sh deploy
```

```
--tasks 4 --parallelism 4     4 tasks, all at once
--cpu 2 --memory 4Gi          Chromium is the bottleneck and wants headroom
--task-timeout 3600s          generous; see the sizing note below
--max-retries 3               safe, because generation is deterministic
```

The four tasks run the **identical command**. They are not assigned ranges and
do not coordinate with each other — each one loops, claiming the next pending
unit from Firestore until none are left. The atomic claim is the only
coordination in the system: no broker, no leader, no work queue service. See
[concurrency.md](concurrency.md).

**Retries are safe** rather than merely tolerated. A document is a pure function
of `(run_id, index)`, and blob writes overwrite in place, so a retried unit
reproduces byte-identical output at the same keys. A crashed task's unit is also
recovered by its **lease** expiring.

### Sizing

- `--unit-size 500` → 50 units over 4 tasks ≈ 12 each. Several units per task
  means one slow unit cannot leave a task idle at the tail. Don't set
  `--unit-size 6250`: four units, no slack, and a single failure costs 25% of
  the run.
- Rendering is roughly 1–4 PDFs/second/task depending on archetype (the
  telecom itemised bill is far heavier than a receipt). 6,250 documents per task
  is therefore **~30–100 minutes**. The 1-hour task timeout may be tight — raise
  it if a task is killed:

  ```bash
  TASK_TIMEOUT=7200s ./deploy.sh deploy
  ```

---

## 5. Run it

```bash
./deploy.sh run          # executes and waits
```

Watch it:

```bash
./deploy.sh logs
./deploy.sh status       # unit counts from Firestore
gcloud storage ls gs://crawler-rag-data-2026-docloom/runs/test-25k/documents/ | head
```

Output lands as:

```
runs/test-25k/documents/unit-000000/inv_00000000.pdf
runs/test-25k/golden/invoices/unit-000000.jsonl.gz
runs/test-25k/golden/line_items/unit-000000.jsonl.gz
```

Every run gets its own prefix, and documents are bucketed per unit — 50 folders
of 500 rather than one directory of 25,000.

### If something fails

A failed unit is left out of the claimable pool so a task moves on rather than
hot-looping. Retry them, and reclaim anything a killed task abandoned:

```bash
./deploy.sh resume
```

This is safe to run repeatedly: completed units are untouched, and only failed
or lease-expired ones re-enter the pool.

---

## 6. Export the golden data

```bash
SINK_URI='bigquery://crawler-rag-data-2026/docloom_golden?staging=gs://crawler-rag-data-2026-docloom/staging' \
  ./deploy.sh export
```

First create the dataset and grant the job BigQuery access:

```bash
bq --location=US mk -d crawler-rag-data-2026:docloom_golden
gcloud projects add-iam-policy-binding crawler-rag-data-2026 \
  --member="serviceAccount:docloom-run@crawler-rag-data-2026.iam.gserviceaccount.com" \
  --role=roles/bigquery.dataEditor
gcloud projects add-iam-policy-binding crawler-rag-data-2026 \
  --member="serviceAccount:docloom-run@crawler-rag-data-2026.iam.gserviceaccount.com" \
  --role=roles/bigquery.jobUser
```

BigQuery reads the staged Parquet as an **external table**, so Parquet
`decimal128` lands as `NUMERIC` and the cent-exact evaluation join survives the
round trip — the same SQL that runs against a local DuckDB file.

Or keep it local and skip BigQuery entirely:

```bash
gcloud storage cp -r gs://crawler-rag-data-2026-docloom/runs/test-25k ./out/
docloom export --run-id test-25k --storage ./out --sink duckdb://./golden.db
```

---

## 7. Cost

For the 25k run:

| | |
|---|---|
| Cloud Run | 4 tasks × ~1h × 2 vCPU / 4 GiB ≈ **$0.80** |
| Cloud Build | a few minutes, within the free daily quota |
| GCS storage | ~2.5 GB of PDFs ≈ **$0.05/month** |
| Firestore | ~150 writes — **free tier** |
| Artifact Registry | one image, ~2 GB ≈ **$0.20/month** |
| **Total** | **~$1 for the run** |

Delete the output when you are done with it:

```bash
./deploy.sh teardown     # prompts; removes the job and runs/ contents
```

---

## Known sharp edges

**Concurrent cold start races the run plan.** All four tasks start at once, and
each checks "does this run exist?" before creating it. Two can both see "no" and
both plan the run, the second resetting units the first had already claimed.

*Effect is bounded:* generation is deterministic and every write is an
idempotent overwrite, so the worst case is **duplicated work, not corrupted
output** — the same documents at the same keys. For a 25k test run it is
noise.

*Avoid it entirely* by planning the run before scaling out:

```bash
TASKS=1 PARALLELISM=1 ./deploy.sh deploy && ./deploy.sh run   # let it start, then cancel
TASKS=4 PARALLELISM=4 ./deploy.sh deploy && ./deploy.sh resume
```

A proper fix — a `plan`-only command, or gating creation on
`CLOUD_RUN_TASK_INDEX=0` — is tracked in `TODO.md`.

**`--total` must match across executions.** It is the run's plan. Changing it on
a resume against an existing `run_id` will not re-plan; use a fresh `RUN_ID`.

**Task timeout kills mid-unit.** Recoverable via the lease, but wasted work.
Prefer a generous timeout over a tight one.

**Region and Firestore location are set once.** A bucket's location and the
`(default)` database's location cannot be changed later — only recreated.
