"""Export — golden shards to a queryable sink.

The second half of the round trip. Generation writes gzipped-JSONL golden shards
to blob storage as it runs; export reads them back and lands them in a
:class:`GoldenSink` (Parquet, DuckDB, or BigQuery), where the evaluation query
joins them against an extractor's output.

Document-agnostic: the tables are *discovered* from the shard layout
(``{run_id}/golden/{table}/…``), not declared, so export needs neither the pack
nor the record schema. The golden codec restores exact ``Decimal`` and ``date``
types on the way in, so the sink rebuilds Parquet ``decimal128`` from real
Decimals and the cent-exact join survives the whole trip — generation → JSONL →
Parquet → SQL — unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from docloom.core.pipeline.golden import decode_shard
from docloom.core.sinks.base import GoldenSink
from docloom.core.storage.base import BlobStore


@dataclass(slots=True)
class ExportStats:
    """Row counts per exported table."""

    tables: dict[str, int] = field(default_factory=dict)

    @property
    def total_rows(self) -> int:
        return sum(self.tables.values())


def export_run(run_id: str, blob: BlobStore, sink: GoldenSink) -> ExportStats:
    """Read a run's golden shards and write them to ``sink``.

    Shards are grouped by table and written in key order, so a table's Parquet
    parts land deterministically. ``sink.register`` is called once at the end to
    make the tables queryable (create DuckDB views / BigQuery external tables;
    a no-op for plain Parquet).
    """
    prefix = f"{run_id}/golden/"
    shards_by_table: dict[str, list[str]] = {}
    for key in blob.iter_keys(prefix):
        table = key[len(prefix):].split("/", 1)[0]
        shards_by_table.setdefault(table, []).append(key)

    stats = ExportStats()
    for table in sorted(shards_by_table):
        count = 0
        for key in sorted(shards_by_table[table]):
            rows = decode_shard(blob.get(key))
            if rows:
                sink.write(table, rows)
                count += len(rows)
        stats.tables[table] = count

    sink.register()
    return stats
