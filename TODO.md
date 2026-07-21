# TODO

Tracked follow-ups that are deliberately deferred, not forgotten.

## Before / soon after first public push
- [ ] **Add a `LICENSE` file.** MIT is the conventional pick for a library like
      this. `pyproject.toml` should then set `license = "MIT"` and a
      `classifiers` entry. No licence file is committed yet, so the repo is
      currently "all rights reserved" by default — add one before promoting it.
- [ ] Review the README framing given the repo generates realistic synthetic
      financial documents: make the testing/eval intent unmistakable up front.

## Concurrency & multi-cloud portability
- [ ] **Lease + reclaim for crashed workers.** The atomic claim marks a unit
      `running` with no lease or heartbeat, so a *silently crashed* worker (vs.
      an explicit `fail_unit`) leaves its unit stuck in `running` forever —
      nothing reclaims it. Add a `lease_expires_at` stamped at claim time and a
      reclaim query that returns expired-lease `running` units to the pool.
      Small change per StateStore adapter (one column + one query). **Required
      before running on AWS/GCP spot/preemptible instances**, which can be
      terminated mid-unit.
- [ ] **DynamoDB StateStore adapter (`dynamodb://`).** The AWS-native networked
      state store for multi-instance runs (AWS Batch, ECS/Fargate, many EC2).
      DynamoDB conditional writes (`ConditionExpression`) give the atomic claim
      the same way Firestore transactions do. Slots behind the existing
      `StateStore` protocol; no pipeline change. (Postgres/RDS via
      `SELECT … FOR UPDATE SKIP LOCKED` is the alternative.)

## Documentation
- [ ] **Deployment & configuration guide** covering every option discussed:
      - **Local:** single box, `sqlite://` + `file://` + `duckdb://`, no cloud.
      - **GCP:** Cloud Run Jobs (task parallelism) / Service, `gs://` +
        `firestore://` + `bigquery://`. This is the reference stack.
      - **AWS:** AWS Batch (array jobs) or ECS/Fargate (task count) or bare EC2;
        `s3://` + `dynamodb://` (or `firestore://`) + Athena/BigQuery. Note the
        single-box-SQLite vs networked-store distinction, and the spot/lease
        caveat above.
      - **Azure equivalent:** Azure Container Apps Jobs (≈ Cloud Run Jobs) or
        Azure Batch; Blob Storage (`az://`), Cosmos DB or Table Storage for
        state, Synapse/Fabric or DuckDB-over-Blob for the golden sink. Note
        which adapters exist vs. still need writing.
      - Per-platform: how workers are launched/scaled, the StateStore
        reachability requirement, and the config URIs for each.
- [ ] **Concurrency & sharding architecture doc.** Write up the model: work
      units = contiguous index ranges = golden shard boundary = export
      granularity; deterministic `hash(run_id, index)` seeding makes units
      independent and reproducible; the atomic claim as the single coordination
      point (pull-based, no broker/leader); two concurrency levels
      (inter-instance units + intra-instance parallel rendering); pause/resume/
      cancel via run-state gating; failed-unit reclaim on resume; the
      large-invoice threshold routing. Explain *why* the compute layer is
      swappable — coordination lives in the StateStore, not the platform — and
      cross-link the deployment guide. (Captures the reasoning from the
      concurrency Q&A so it isn't lost to chat history.)

## Backlog
- [ ] Cloud adapters end-to-end verification against emulators
      (fake-gcs-server, Firestore emulator) and a real BigQuery project.
- [ ] **`(cont'd)` markers on page-spanning sections** (telecom archetype).
      Needs post-layout knowledge of where breaks landed, which Chromium does
      not expose simply. The repeating `<thead>` already gives an extractor the
      structural continuation cue, so this is a realism nicety, deferred. CSS
      hides the empty placeholder in the meantime.
- [ ] **Move running-header composition into the pack.** The PDF renderer reads
      `issuer`/`invoice_number` off the record via getattr — invoice-specific.
      When a second document type needs a different header, add a pack hook
      (e.g. `header_fields(record)`) rather than growing the getattr list.
- [ ] Remaining invoice archetypes (~13) from the source corpus.
- [ ] Catalogue runner (drives the provider mix over ~70k items under budget;
      Anthropic Batch API for the offline Haiku slice) + logo generation.
- [ ] Export mode (JSONL shards → GoldenSink) + `docloom generate|export` CLI.
- [ ] Scan-degradation + handwriting variants (post-process rendered PDFs).
