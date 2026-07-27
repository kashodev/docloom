# Pipeline

`core/pipeline/` is the document-agnostic spine of a run: plan → create → drive →
render → persist → manifest, then export. It calls the pack only through the
[contract](../architecture/contract.md) hooks.

| Module | Role |
|---|---|
| `run.py` | Orchestration — `create_run`, `work_run`, `resume_run`. |
| `planner.py` | `plan_units` — a document count → a list of work units (index ranges). |
| `worker.py` | `GenerationWorker` — the drain loop: claim → generate → render → persist per unit. |
| `source.py` | `DocumentSource` protocol, `stable_seed`, `prepare_source`. |
| `renderer.py` | `DocumentRenderer` protocol, `HtmlRenderer`; `render()` → bytes. |
| `pdf.py` | `PdfRenderer` — Chromium/Playwright print; running header; page-fit. |
| `degrade.py` | `degrade_pdf` — scan post-processing (rasterise, noise, JPEG, no text layer). |
| `golden.py` | `encode_shard` / `decode_shard` — the exactness-preserving golden codec. |
| `manifest.py` | `UnitManifest` / `RunManifest` — the self-describing per-run manifest. |
| `export.py` | `export_run` — golden shards → a sink (see [Export](../invoice/export.md)). |

## Run orchestration (`run.py`)

Three entry points, all called by a CLI command or a Cloud Run task:

- **`create_run`** plans the run into units and records it. Safe to call from every
  worker at once — creation is a **conditional write**, so exactly one worker plans
  and the rest wait for the plan to land (`Run.planned` closes the follow-on race).
- **`work_run`** drives one worker until the unit pool is empty. Multiple processes
  calling it against the same store *is* the multi-worker case; the atomic claim
  keeps them from colliding. On the transition to `COMPLETED` (and only then) it
  writes the **root manifest** — so the root's presence is the completion signal.
- **`resume_run`** clears any pause, returns failed units to the pool, and reclaims
  units abandoned by crashed workers (expired leases).

A run's identity lives in the **state store**, not a Python object, so any process
that can reach the store can start, resume, pause, or cancel it.

## The worker loop (`worker.py`)

`GenerationWorker.run()` claims the next unit and processes it until none remain.
For each document index in a unit it: `source.generate(run_id, index)` → a
`GoldenRecord`; `renderer.render(record)` → bytes; `blob.put(key, …)`;
`record.to_rows()` → golden shards; and — **before** marking the unit done —
writes the unit's manifest part. That order is load-bearing: a completed unit
always has a manifest part, so the root manifest can trust the parts. A failed
document marks the unit failed and the worker moves on (retried on the next
resume, not in a hot loop).

## Determinism (`source.py`)

`stable_seed(run_id, index)` is a SHA-256 over `(run_id, index)` — every draw an
invoice makes is seeded from it, so the same index always yields the same
document. That is what makes a run **resumable** (re-generating an index
overwrites identical content) and the golden set **reproducible**.
`prepare_source` runs a source's one-time, run-scoped setup (e.g. resolving a
`Selection` against the roster) once, up front, so a bad configuration stops the
run rather than failing document after document.

## Rendering (`renderer.py`, `pdf.py`, `degrade.py`)

`HtmlRenderer` produces the HTML via the kernel's Jinja environment
(`render_record(pack, record)`). `PdfRenderer` prints that through
Chromium/Playwright, using `header_fields(record)` for the running header and
handling page-fit (`fit_scale`) and `(cont'd)` markers on page-spanning sections
(`spanning_sections`). `degrade.py` then realises the capture condition: **clean**
passes through with its text layer intact; a **scan** rasterises at 150 dpi,
degrades with the record's own seed, and re-wraps with **no text layer** so OCR
must read pixels. (Handwritten pads are rendered by the invoice pack; see
[Invoice generation](../invoice/generation.md).)

## Golden shards & manifest (`golden.py`, `manifest.py`)

`golden.py` encodes a unit's rows to a **gzipped-JSONL shard**, preserving exact
values (money as strings, not floats). `manifest.py` writes a **part per unit**
(`<prefix>/manifest/unit-XXXXXX.json`, listing that unit's documents and shards
with a sha256 each) and, at completion, a **root** (`<prefix>/manifest.json`)
indexing the parts plus totals. The root refuses to write over a missing part,
and its presence is the run's done-signal — a partial run has parts but no root.
A consumer can reconstruct and verify a run from the manifest alone
(`read_run_manifest`, `is_complete`, `verify_run`, `enumerate_document_keys`).

See [Concurrency & coordination](../architecture/concurrency.md) for the claim /
lease / completion contract, and [Execution flows](../architecture/flows.md) for
the end-to-end sequences.
