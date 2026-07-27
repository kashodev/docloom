# Storage / Sinks / Usage

Three more URI-scheme backend groups, each a small protocol with interchangeable
implementations chosen by config (**② provider-agnostic**). The generation and
export code is identical across them.

## Blob store (`core/storage/`)

Where rendered documents and golden shards land. `open_store(uri)` →
`file://` · `gs://` · `s3://`.

```python
class BlobStore(Protocol):
    def put(self, key, data, content_type="application/octet-stream") -> str: ...
    def get(self, key) -> bytes: ...
    def exists(self, key) -> bool: ...
    def iter_keys(self, prefix="") -> Iterator[str]: ...
    def uri_for(self, key) -> str: ...
```

Keys are deterministic and unit-sharded, e.g.
`<prefix>/documents/unit-000123/<id>.pdf` and
`<prefix>/golden/<table>/unit-000123.jsonl.gz`, where `<prefix>` defaults to the
run id (a multi-slice run nests under `runs/<run>/<slice>/…`). `iter_keys` is what
[export](../invoice/export.md) and manifest verification walk. Cloud backends need
the matching extra (`docloom[gcp]` / `[aws]`); a missing one raises an actionable
error naming the install.

## Golden sinks (`core/sinks/`)

Export targets. `open_sink(uri)` → `parquet://` · `duckdb://` · `bigquery://`.

```python
class GoldenSink(Protocol):
    def write(self, table, rows) -> str: ...   # append rows to a table
    def register(self) -> None: ...            # make the tables queryable (views / external tables)
    def close(self) -> None: ...
```

`export_run` calls `write` per shard in key order (deterministic Parquet parts)
and `register` once at the end. Rows are plain dicts converted through **Arrow**;
money stays exact (decimal, not float). DuckDB adds views over the Parquet;
BigQuery stages Parquet in GCS and creates external tables; plain Parquet's
`register` is a no-op. See [Export](../invoice/export.md).

## Usage telemetry (`core/usage/`)

Records what every LLM call cost — model, tokens, exact `Decimal` cost, and what
it was generating — as its own `llm_usage` table. **On by default** (a procedural
pack makes no calls, so it writes nothing). `open_usage_sink(uri)` → `shard://`
(default, gzipped-JSONL beside the golden data) · `firestore://` · `dynamodb://` ·
`off`.

```python
class UsageSink(Protocol):
    def record(self, usage: LlmUsage) -> None: ...
    def flush(self) -> int: ...
    def close(self) -> None: ...
```

It is deliberately **not** golden data — token counts aren't reproducible, and the
golden record's value is that it is exactly recomputable. A replayed unit
**overwrites** its rows (shard key / deterministic id / sort key), so retries
can't double-count. Because `export_run` discovers tables by prefix, the
`shard://` usage exports to Parquet/DuckDB/BigQuery with no export change.

Distinct from the **spend rollup** in the [state store](state.md): the rollup is a
live per-(run, model) counter a budget guard reads across a fleet; `llm_usage` is
the analytics fact table. The two agree on how a run is identified.
