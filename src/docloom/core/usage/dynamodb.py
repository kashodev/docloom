"""DynamoDB usage sink (``dynamodb://table``).

For AWS deployments already using ``dynamodb://`` for run state. Uses the same
single-table shape as :mod:`docloom.core.state.dynamodb` — partition key ``pk``,
sort key ``sk`` — so one table can hold run state and usage side by side, and the
keys sort usefully: every row for a run is one Query, ordered by unit.

Sort keys are **deterministic** (``USAGE#<unit>#<sequence>``), so a retried unit
overwrites its own rows instead of appending duplicates. Spend counted twice is
the classic failure of bolt-on telemetry; here it cannot happen.

**Know what this is for.** DynamoDB has no aggregation: "sum cost by model over
30 days" is a full ``Scan``, which grows linearly and costs real money and
minutes. Prefer :mod:`docloom.core.usage.shard` for the fact table and reach for
this when the operational win — one datastore — is worth the query cost, or for
reduced-granularity rollups where the row count is small.

boto3 is lazy-imported and the table resource injectable, so this module loads
without it and its item mapping is unit-tested without it.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from docloom.core.usage.base import TABLE, LlmUsage

_USAGE_PREFIX = "USAGE#"
#: Zero-padded so lexicographic sort-key order is numeric order, matching the
#: state store's `UNIT#…` convention.
_UNIT_WIDTH = 6
_SEQ_WIDTH = 8


def usage_sort_key(unit_index: int | None, sequence: int) -> str:
    """Deterministic sort key. A catalogue call has no unit, so it sorts under a
    ``cat`` bucket rather than colliding with unit 0."""
    unit = "cat" if unit_index is None else f"{unit_index:0{_UNIT_WIDTH}d}"
    return f"{_USAGE_PREFIX}{unit}#{sequence:0{_SEQ_WIDTH}d}"


def usage_to_item(usage: LlmUsage, sequence: int) -> dict[str, Any]:
    """Row → DynamoDB item.

    DynamoDB's number type *is* arbitrary-precision decimal, so unlike Firestore
    the cost survives exactly — but boto3 rejects ``float``, and only accepts
    ``Decimal``, which is what this data already carries. ``None``s are dropped:
    absent reads back cleanly and costs nothing to store.
    """
    item: dict[str, Any] = {
        "pk": usage.run_id,
        "sk": usage_sort_key(usage.unit_index, sequence),
        **{k: v for k, v in usage.to_row().items() if v is not None},
    }
    item["cost_usd"] = usage.cost_usd if isinstance(usage.cost_usd, Decimal) else Decimal(
        str(usage.cost_usd)
    )
    return item


class DynamoDbUsageSink:
    """LLM usage rows in a DynamoDB table."""

    scheme = "dynamodb"

    def __init__(
        self,
        table: str = TABLE,
        *,
        region: str | None = None,
        endpoint_url: str | None = None,
        resource: Any | None = None,
        table_resource: Any | None = None,
    ) -> None:
        self._buffer: list[LlmUsage] = []
        self._written = 0
        if table_resource is not None:
            self._table = table_resource
            return
        if resource is None:
            try:
                import boto3
            except ImportError as exc:
                raise ImportError(
                    "dynamodb:// usage needs the AWS extra — pip install 'docloom[aws]'"
                ) from exc
            kwargs: dict[str, Any] = {}
            if region:
                kwargs["region_name"] = region
            if endpoint_url:
                kwargs["endpoint_url"] = endpoint_url
            resource = boto3.resource("dynamodb", **kwargs)
        self._table = resource.Table(table)

    def record(self, usage: LlmUsage) -> None:
        self._buffer.append(usage)

    def flush(self) -> int:
        if not self._buffer:
            return 0
        written = 0
        # batch_writer handles the 25-item limit, retries and unprocessed items.
        with self._table.batch_writer() as batch:
            for usage in self._buffer:
                batch.put_item(Item=usage_to_item(usage, self._written + written))
                written += 1
        self._buffer.clear()
        self._written += written
        return written

    def close(self) -> None:
        self.flush()
