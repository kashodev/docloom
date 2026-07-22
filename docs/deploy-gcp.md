# Running docloom on GCP

A runnable walkthrough of the reference stack: Cloud Run Jobs for compute, GCS
for documents and golden shards, Firestore for run state. Everything is
`gcloud`, driven by [`deploy/gcp/deploy.sh`](../deploy/gcp/deploy.sh) and a
config file.

The general model — which platform maps to what, and why — is in
[deployment.md](deployment.md). This is the specific version.

**One script, one config shape, any size of run.** A 1,000-document smoke test
and a 5,000,000-document production run differ only in numbers:

```bash
./deploy.sh -c run.yaml --set run.id=smoke --set run.total=1000 --set job.tasks=1 all
./deploy.sh -c run.yaml all                       # whatever the file says
```

Nothing about "test" is baked into the tooling. Start from
[`run.example.yaml`](../deploy/gcp/run.example.yaml), which is commented
throughout, and copy it per run.

## The config

| Block | Controls |
|---|---|
| `project` / `region` / `bucket` / `firestore` | where it runs, what it writes to |
| `run` | `total`, `unit_size`, `format`, `config_id`, `max_line_items`, telemetry URI |
| `documents` | **which document types, and their split as percentages** |
| `catalogue` | **which LLMs, their percentage split, and the budget** |
| `job` | tasks, parallelism, CPU, memory, timeout, retries |
| `export` | the golden sink URI |

Override anything per invocation without editing the file. Types are preserved,
so `run.total` stays an integer:

```bash
./deploy.sh -c run.yaml --set run.total=1000 --set job.tasks=2 plan
```

**`plan` changes nothing** and is the quickest way to see whether a config is
sane. It resolves the document split, computes units, warns when there are fewer
units than tasks, and validates that both percentage splits total 100:

```
  documents       1000000 total, unit size 1000, format pdf
    invoice        500000 docs (50%)  →  500 units  run id: prod-2026-07-invoice
    contract       500000 docs (50%)  →  500 units  run id: prod-2026-07-contract

  job             docloom-generate: 200 task(s), parallelism 200
  units per task  ~5 of 1000
```

### Document slices — size *and* composition

A slice is a batch with its own size and its own make-up. This is the requested
shape — one vendor on one template, a mixed batch, a French batch, a handwritten
batch — expressed directly:

```yaml
documents:
  - name: anchor-vendor          # 10k from one company on one template
    count: 10000
    companies: [anchor]
    archetypes: [meta-sidebar-01]

  - name: mixed                  # 10k across every template and the roster
    count: 10000
    archetypes: all

  - name: french                 # 2.5k French, both variants
    count: 2500
    locales: [fr-CA, fr-FR]

  - name: handwritten            # 2.5k hand-filled, small pool of hands/layouts
    count: 2500
    condition: handwritten
    locales: [en-US]
    companies: 10
    archetypes: 3
```

Size with **either** `count` (absolute) or `share` (percent of `run.total`) —
mixing the two is rejected. With `count`, `run.total` may be omitted and is
taken as the sum; if you give both they must agree.

| Field | Meaning |
|---|---|
| `name` | slice id; becomes the run id suffix. Defaults to `slice-1`, `slice-2`, … |
| `pack` | document type; defaults to `invoice` |
| `count` / `share` | size |
| `locales` | restrict to `en-US en-CA en-GB fr-CA fr-FR` |
| `companies` | list of company ids to pin to, **or** an integer "use N companies" |
| `archetypes` | list of template names, an integer "use N", or `all` |
| `condition` | `clean` · `light_scan` · `heavy_scan` · `handwritten` |
| `format` | `pdf` · `html`; defaults to `run.format` |
| `goods_receipt` | delivery note with a receiver's signature (implies handwritten) |

**Everything is optional.** Omit a field and the slice is unconstrained on that
axis — the sampler's normal behaviour. Omit `documents` entirely and you get one
unconstrained invoice slice for the whole of `run.total`.

Vocabularies are validated, so a typo fails in `plan` rather than forty minutes
into a run:

```
$ ./deploy.sh -c run.yaml plan
unknown locales: fr_FR (known: en-CA, en-GB, en-US, fr-CA, fr-FR)
```

**Each slice is its own run** (`<run.id>-<name>`), so slices are independently
resumable, exportable and queryable — `run_id` is on every golden row. That also
follows from the model: a run records exactly one pack in its plan.

> **⚠ Composition is declared, not yet enforced.** `count`, `pack`, `format` and
> `name` are honoured today. `locales`, `companies`, `archetypes`, `condition`
> and `goods_receipt` are validated and shown by `plan`, but **the generator
> cannot be told to obey them**: `docloom generate` has no such flags, and the
> sampler draws companies from the full weighted roster, taking each company's
> own locale and template. **A slice named `french` will not be French until
> that lands.**
>
> The seam exists — `InvoiceSampler` already takes `goods_receipt=True` and
> filters its roster on it — so this is roster/product filtering exposed as CLI
> flags, not new machinery. Tracked in `TODO.md`.
>
> Meanwhile the intent is recorded as `--config-id` on each run, and what you
> *actually* got is queryable: `locale`, `company_id`, `condition` and
> `is_handwritten` are all columns on the golden row.

### Which LLMs, and their split

```yaml
catalogue:
  budget_usd: 50.00
  providers:
    - {name: deepseek,  model: deepseek-v4-flash, weight: 40}
    - {name: dashscope, model: qwen3.5-flash,     weight: 40}
    - {name: anthropic, model: claude-haiku-4-5,  weight: 20}
```

Weights are percentages of catalogue items routed to each provider and must total
100. Blending models mixes their voices so an extraction pipeline cannot learn a
single generator's style — a *quality* decision, not only a cost one. Routing is
deterministic per item, so a rerun draws the same model for the same item and a
weak slice can be regenerated without reshuffling authorship.

> **⚠ Configurable and validated, but not yet executable.** There is no
> `docloom catalogue` command, so the script checks this block and prints it but
> cannot run it. That is not a gap in the script: **document generation makes no
> LLM calls at all.** A procedural pack computes its content; an LLM-backed pack
> reads a catalogue built *earlier*, offline. The provider mix belongs to that
> earlier step, and exposing it is tracked in `TODO.md`.

### The worked example below

Uses the example file's defaults:

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
cp run.example.yaml run.yaml     # then edit it
./deploy.sh -c run.yaml plan     # sanity-check before creating anything
./deploy.sh -c run.yaml provision
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
./deploy.sh -c run.yaml build
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
./deploy.sh -c run.yaml deploy
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
  ./deploy.sh -c run.yaml --set job.task_timeout=7200s deploy
  ```

---

## 5. Run it

```bash
./deploy.sh -c run.yaml run      # one execution per document type; waits for each
```

Watch it:

```bash
./deploy.sh -c run.yaml logs
./deploy.sh -c run.yaml status   # unit counts from Firestore, per run
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
./deploy.sh -c run.yaml resume
```

This is safe to run repeatedly: completed units are untouched, and only failed
or lease-expired ones re-enter the pool.

---

## 6. Export the golden data

Set `export.sink` in the config:

```yaml
export:
  sink: "bigquery://crawler-rag-data-2026/docloom_golden?staging=gs://crawler-rag-data-2026-docloom/staging"
```

```bash
./deploy.sh -c run.yaml export     # one export per document type
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
./deploy.sh -c run.yaml teardown   # prompts; removes the job and this run's output
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
./deploy.sh -c run.yaml --set job.tasks=1 --set job.parallelism=1 deploy
./deploy.sh -c run.yaml run          # let it plan the run, then cancel the execution
./deploy.sh -c run.yaml deploy       # back to the configured task count
./deploy.sh -c run.yaml resume
```

A proper fix — a `plan`-only command, or gating creation on
`CLOUD_RUN_TASK_INDEX=0` — is tracked in `TODO.md`.

**`--total` must match across executions.** It is the run's plan. Changing it on
a resume against an existing `run_id` will not re-plan; use a fresh `RUN_ID`.

**Task timeout kills mid-unit.** Recoverable via the lease, but wasted work.
Prefer a generous timeout over a tight one.

**Region and Firestore location are set once.** A bucket's location and the
`(default)` database's location cannot be changed later — only recreated.
