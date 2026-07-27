# Steps — catalog / pdfs / export

The studio runs the same three things the CLI does, mapped onto the
[kernel flows](../architecture/flows.md). Each is a `--step`, with args prompted
interactively or passed as flags.

## `catalog` — build the content catalogue

Builds the invoice pack's [catalogue](../invoice/catalogue.md). Procedural by
default (key-free); an [LLM provider mix](catalogue.md) writes richer descriptions
under a budget.

Args: `--version`, `--companies`, `--products-per-company`, `--mix`, `--budget`,
`--concurrency`, `--tasks`. On a cloud target it writes to
`gs://<bucket>/catalogues/<pack>/<version>`; the studio [detects an existing
catalogue](catalogue.md#re-use) and reuses it rather than rebuild (a rebuild costs
money).

## `pdfs` — generate documents

The run: render N documents + the golden set. A **single inline slice** is prompted;
a full multi-slice composition points at a `run.yaml` (`--config`).

Args: `--run-id`, `--total`, `--catalogue`, `--format`, `--condition`,
`--issue-date-from/-to`, `--tasks`, `--parallelism`. On `gcp` the run is
**[dispatched detached](dispatch.md)** (`deploy.sh deploy + dispatch`) and returns
handles + links immediately; on `local` it runs synchronously.

## `export` — golden shards → a sink

Loads a completed run's golden shards into a queryable sink
([Export](../invoice/export.md)).

Args: `--run-id`, `--sink` (default `bigquery://<project>/golden` on gcp, a local
DuckDB on `local`).

## Running them

- Interactive: `docsynth studio` → pick the target, project, and step; confirm.
- Flagged: `docsynth studio -p gcp --project <ref> --step catalog --version v2 --mix cheap-mix --budget 5 --yes`.
- Preview: add `--dry-run` to print the resolved `deploy.sh` command without running,
  or [`--scaffold <path>`](tuning.md) to write a reusable `run.yaml`.

The wizard loops back to the step menu after each run, so a session can catalog →
pdfs → export against one project.
