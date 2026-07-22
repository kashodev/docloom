# Concurrency & sharding

How docloom runs one job across many workers — on a laptop or a cloud fleet —
without a broker, a leader, or a chance of generating the same document twice.
This captures the *why* behind the pipeline; the per-platform *how* (which
compute service, which URIs) lives in [deployment.md](deployment.md).

## The unit is the only number you reason about

A run of `total` documents is divided by [`plan_units`](../src/docloom/core/pipeline/planner.py)
into contiguous **work units** of `unit_size` document indices — unit 0 is
`[0, unit_size)`, unit 1 the next range, and so on, the last unit absorbing the
remainder. That single boundary is deliberately three things at once:

- **the concurrency claim** — a worker claims one whole unit at a time;
- **the golden shard** — one unit produces one shard per table (`…/golden/invoices/0000.jsonl.gz`);
- **the export read granularity** — the sink reads shard by shard.

Because the three coincide, there is one knob (`unit_size`) that trades
parallelism granularity against per-unit overhead, and no separate sharding or
batching scheme to keep in sync.

A unit carries **only its index range** — `start_index` and `count` — never any
document content. That is what makes the whole model cheap and stateless to
coordinate.

## Units are independent and reproducible

Every document is generated from a process-stable seed,
[`stable_seed(run_id, index)`](../src/docloom/core/pipeline/source.py) — a
SHA-256 of the run id and the document index, **not** Python's salted built-in
`hash()`, which varies between processes and would break reproducibility across
workers.

Two consequences follow:

- **Independence.** A unit needs nothing from any other unit. There is no shared
  running counter, no cross-unit ordering, no handoff — so units can be worked in
  any order, on any machine, at the same time.
- **Reproducibility.** A unit is fully determined by its range. Regenerating
  `[3000, 4000)` on a different worker, a week later, yields byte-for-byte the
  same documents and the same golden rows. Retrying a failed unit is therefore
  safe and exact, and a run is auditable from its id alone.

## The atomic claim is the single coordination point

Coordination is **pull-based**. Workers do not get assigned work; each one loops,
[claiming the next pending unit](../src/docloom/core/pipeline/worker.py) and
processing it until the pool is empty:

```
while (unit := state.claim_next_unit(run_id)) is not None:
    process(unit)
```

`claim_next_unit` is atomic: it takes the lowest-index pending unit and marks it
`running` in one indivisible step, so two workers racing on the same unit get
different units, or one gets `None`. That, plus the conditional *create* that
[plans the run](#planning-a-run-from-many-workers-at-once), is the entire
coordination mechanism — there is no broker, no leader election, no work queue
service.

The atomicity lives in the **StateStore**, not in the compute layer:

- [`SqliteStateStore`](../src/docloom/core/state/sqlite.py) serialises workers
  with a `BEGIN IMMEDIATE` write lock + `UPDATE … RETURNING` — enough for one box
  with many processes/threads.
- [`FirestoreStateStore`](../src/docloom/core/state/firestore.py) serialises them
  with a document transaction that retries on contention — enough for many cloud
  instances against one run.
- [`DynamoDbStateStore`](../src/docloom/core/state/dynamodb.py) serialises them
  with a conditional write (update this unit *only if* it is still `pending`), so
  exactly one racing worker wins and the losers advance to the next candidate —
  the AWS-native equivalent.

Same protocol, same guarantee, different scale. **This is why the compute layer
is swappable**: nothing about "who runs the workers" carries coordination state,
so the workers can be laptop processes, Cloud Run Job tasks, AWS Batch array
elements, or ECS tasks, and the correctness argument does not change. Point the
`state://` URI at a store every worker can reach and you have a fleet.

## Planning a run from many workers at once

The claim protocol assumes the plan already exists. Getting there is its own
race: N tasks start simultaneously — which is exactly what a Cloud Run Job or an
AWS Batch array job does — and every one of them calls `create_run` for the same
`run_id`. Nobody is "first" in any way the workers can observe.

So **creation is conditional too**, using the same primitive as the claim:

| Store | Conditional create |
|---|---|
| SQLite | `INSERT … ON CONFLICT(run_id) DO NOTHING` |
| Firestore | `document.create()` — fails if the document exists |
| DynamoDB | `put_item(ConditionExpression="attribute_not_exists(pk)")` |

`create_run` returns **whether this caller wrote the plan**. Exactly one worker
gets `True`; the rest get `False` and wait for that plan rather than writing over
it. Before this, a late planner would reset units an earlier worker had already
claimed, so the same documents were generated twice (and on SQLite the loser died
on a `UNIQUE` violation).

### `planned` — because the marker and the units are not one write

SQLite writes the run row and every unit in one transaction, so it is never
half-planned. Firestore and DynamoDB cannot do that at this size, which leaves a
window where the run marker exists but its units do not.

That window is worse than it sounds. A worker reading a marker with no units
behind it sees "a run with nothing to claim" — indistinguishable from a finished
run. It would claim nothing, exit **successfully**, and report a completed run
that generated no documents. A duplicate-work bug is expensive; this one is
silent.

So the marker goes down first carrying `planned: False`, and only flips to `True`
once every unit is written. `claim_next_unit` returns `None` for an unplanned
run, and [`create_run`](../src/docloom/core/pipeline/run.py) polls until the flag
flips — raising `TimeoutError` rather than proceeding quietly, for the same
reason.

Two details that matter:

- **Absent means planned.** Rows written before the flag existed only ever became
  visible fully planned, so `planned` defaults to `True`. Defaulting the other
  way would strand every existing run.
- **A dead planner does not wedge the run.** If a marker sits unplanned for
  longer than `PLANNING_TAKEOVER_SECONDS` (120s), the next worker takes the plan
  over. Unit writes are keyed by index and byte-identical, so finishing another
  planner's work is safe.

`docloom plan` creates the plan and exits. It is **not** required — `generate`
plans safely from every worker — but it lets a large run be planned and inspected
with `docloom status` before compute is committed to it.

### Why not gate on the worker ordinal

Cloud Run sets `CLOUD_RUN_TASK_INDEX`, AWS Batch sets
`AWS_BATCH_JOB_ARRAY_INDEX`, and letting only ordinal 0 plan would be a two-line
fix. It was rejected: it moves coordination into the compute layer, which is
precisely what the rest of this document says never happens — and it does nothing
for many processes on one laptop, where there is no ordinal at all.

## Two levels of concurrency

1. **Across instances** — many workers run the same drain loop against one shared
   StateStore. The atomic claim keeps them from colliding. This is the primary
   scaling axis and it is horizontal: add instances, point them at the store.
2. **Within a unit** — a unit's documents are independent of each other too, so
   they can render in parallel inside one worker. The loop is currently
   sequential (HTML generation is cheap); the async PDF renderer is where
   intra-unit parallelism pays off, and it slots into `_generate_unit` without
   touching the persistence or accounting.

The two compose: `instances × intra-unit parallelism` total concurrency.

## Lifecycle: pause, resume, cancel

Run state gates claiming. `claim_next_unit` returns `None` when the run is
`PAUSED` or `CANCELLED`, so **pause/cancel need no signal to the workers** — they
simply stop being handed units and drain to a stop. Resume flips the run back to
`RUNNING` and requeues work:

- **Failed units** — a unit that raised is marked `failed` and left *out* of the
  claimable pool, so a draining worker moves on instead of hot-looping on it.
  [`resume_run`](../src/docloom/core/pipeline/run.py) calls `reset_failed_units`
  to return them to `pending` — a retry is a deliberate act.
- **Abandoned units (crash recovery)** — a worker killed mid-unit
  (spot/preemptible termination, OOM) leaves its unit `running` with nobody to
  finish it. Each claim stamps a **lease** (`lease_expires_at`); once it lapses
  the unit is reclaimable. `resume_run` reclaims on resume, and SQLite also
  reclaims opportunistically inside the claim's own write lock, so a
  continuously draining fleet self-heals without an explicit resume. (Firestore
  and DynamoDB reclaim explicitly — a per-claim scan is too costly at scale, so
  on spot/preemptible fleets run `--resume` periodically or a small sweeper.) See
  [`reclaim_expired_units`](../src/docloom/core/state/base.py).

Because completion is idempotent and reproducible, resuming after any
interruption re-does only what did not finish.

## Spend, and why a budget needs the store too

`BudgetGuard` caps one process. Two hundred workers each honouring a $50 ceiling
will spend $10,000 — every guard behaving correctly, the fleet still over budget.
So spend uses the same trick as everything else here: put the shared fact in the
**StateStore**, which by definition every worker can reach.

`add_spend(run_id, model, cost)` atomically increments a `(run, model)` row and a
`(run, "*")` total, returning the new total — the value a cap must be checked
against, because summing per-model rows would race. It is an atomic upsert in
SQLite, `Increment` in Firestore, `ADD` in DynamoDB.

Spend accumulates as **integer nano-dollars**, not a float: Firestore's
`Increment` is int-or-double, and money must never live in a double. The
authoritative per-call ledger is still the `llm_usage` table; the rollup is the
live aggregate and the enforcement point.

`DistributedBudgetGuard` sits on top with two staleness dials — `flush_every`
(how much of *our* spend to buffer) and `refresh_interval` (how long we may go
without re-reading *others'* spend). Neither can be free: perfect enforcement is
a shared read and write per call. Both bound the overshoot instead, one in calls
and one in wall-clock.

## Large-document routing

Most invoices are a handful of line items, but a few archetypes (the telecom
itemised bill) run to hundreds or thousands of rows and dominate render cost. The
intended refinement is to **route** those: detect the heavy documents and give
them a smaller effective `unit_size` (fewer per unit) so one unit's wall-clock
stays bounded and a single fat unit can't stall a shard. The seam for this is the
planner + source; today `unit_size` is uniform and the sampler caps line counts
via `max_line_items`. Documented here so the boundary — routing is a *planning*
decision, not a worker or record change — is not lost. (Status: planned.)

## Summary

| Concern | Mechanism |
|---|---|
| Work division | contiguous index ranges = shard = export granularity |
| Determinism | `stable_seed(run_id, index)`, SHA-256, process-stable |
| Coordination | atomic `claim_next_unit`, pull-based, no broker/leader |
| Cold start | conditional `create_run` — one planner, the rest wait on `planned` |
| Where atomicity lives | the StateStore (SQLite lock / Firestore txn), not compute |
| Scaling | inter-instance drain loops × intra-unit parallel render |
| Pause / cancel | run-state gating — workers just stop being served |
| Retry / crash recovery | failed-unit reset + expiring lease reclaim on resume |
| Compute portability | coordination is in the store, so the runner is swappable |

See also: [DESIGN.md](../DESIGN.md) for the full architecture, and
[deployment.md](deployment.md) for per-platform wiring.
