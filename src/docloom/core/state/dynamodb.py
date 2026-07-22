"""DynamoDB state store (``dynamodb://table``).

The AWS-native networked state store, for multi-instance runs on AWS Batch,
ECS/Fargate, or plain EC2 — the counterpart to Firestore on GCP. Where SQLite
serialises workers with a local write lock and Firestore with a document
transaction, DynamoDB uses a **conditional write**: the claim updates a unit only
``IF state = 'pending'``, so when two workers race for the same unit exactly one
condition holds and the loser moves to the next candidate. Same protocol, same
guarantee, no broker.

Layout is single-table. Partition key ``pk`` is the run id; sort key ``sk`` is
``RUN`` for the run itself and ``UNIT#00000123`` for each unit — zero-padded so a
plain ascending Query returns units in index order, which is what makes
"lowest-index pending unit" a cheap forward scan.

**On create_run atomicity.** DynamoDB cannot transact more than 100 items and a
run routinely has more units than that, so the plan cannot be one transaction.
Instead the run marker is written **first** with a conditional put
(``attribute_not_exists(pk)``), which serialises planners: exactly one of N
simultaneously-starting workers wins and writes the units. The marker carries
``planned=False`` until they land, so a half-written plan is distinguishable from
a finished one rather than reading as "nothing left to do"; a planner that dies
mid-plan is taken over once its marker goes stale.

Like Firestore, the claim does **not** scan for expired leases — that cost is
paid explicitly by :meth:`reclaim_expired_units`, which resume calls and a
sweeper can run periodically.

boto3 is lazy-imported (an ``docloom[aws]`` extra) and the table resource is
injectable, so this module imports without boto3 and the item mapping is
unit-tested without it; the real conditional claim is covered by a
DynamoDB-Local-gated integration test.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from docloom.core.enums import RunState, WorkUnitState
from docloom.core.state.base import (
    DEFAULT_LEASE_SECONDS,
    PLANNING_TAKEOVER_SECONDS,
    TOTAL_MODEL,
    Run,
    Spend,
    WorkUnit,
    from_nano,
    to_nano,
)

_RUN_SK = "RUN"
_UNIT_PREFIX = "UNIT#"
_SPEND_PREFIX = "SPEND#"
#: Zero-padded so lexicographic sort-key order == numeric unit order.
_UNIT_WIDTH = 8


def spend_sort_key(model: str) -> str:
    """Sort key for a rollup row.

    A distinct prefix keeps rollup rows out of the unit range, so the claim's
    ``begins_with("UNIT#")`` query never sees them and vice versa. (They happen
    to sort *before* the unit rows — ``SPEND#`` < ``UNIT#`` — which is
    immaterial, since both are read by prefix, never by scanning the partition.)
    """
    return f"{_SPEND_PREFIX}{model}"


def item_to_spend(item: dict[str, Any]) -> Spend:
    """DynamoDB item → rollup row. Pure, so the mapping is unit-tested."""
    return Spend(
        run_id=item["pk"],
        model=item.get("model", TOTAL_MODEL),
        cost_usd=from_nano(int(item.get("cost_nano", 0))),
        calls=int(item.get("calls", 0)),
        input_tokens=int(item.get("input_tokens", 0)),
        output_tokens=int(item.get("output_tokens", 0)),
    )


def unit_sort_key(unit_index: int) -> str:
    """Sort key for a unit — zero-padded to keep lexicographic == numeric order."""
    return f"{_UNIT_PREFIX}{unit_index:0{_UNIT_WIDTH}d}"


# ── Pure item mapping (unit-tested without boto3) ───────────────────────────
def run_to_item(run: Run) -> dict[str, Any]:
    return {
        "pk": run.run_id,
        "sk": _RUN_SK,
        "pack": run.pack,
        "config_id": run.config_id,
        "total_units": run.total_units,
        "state": run.state.value,
        "created_at": run.created_at.isoformat(),
        "updated_at": run.updated_at.isoformat(),
        "metadata": run.metadata,
        "planned": run.planned,
    }


def item_to_run(item: dict[str, Any]) -> Run:
    return Run(
        run_id=item["pk"],
        pack=item["pack"],
        config_id=item["config_id"],
        total_units=int(item["total_units"]),
        state=RunState(item["state"]),
        created_at=datetime.fromisoformat(item["created_at"]),
        updated_at=datetime.fromisoformat(item["updated_at"]),
        metadata=dict(item.get("metadata") or {}),
        # Absent on items written before the flag existed; those runs were always
        # fully planned before they became visible.
        planned=bool(item.get("planned", True)),
    )


def unit_to_item(unit: WorkUnit) -> dict[str, Any]:
    return {
        "pk": unit.run_id,
        "sk": unit_sort_key(unit.unit_index),
        "unit_index": unit.unit_index,
        "start_index": unit.start_index,
        "count": unit.count,
        "state": unit.state.value,
        "attempts": unit.attempts,
        "error": unit.error,
        "updated_at": unit.updated_at.isoformat(),
        "lease_expires_at": unit.lease_expires_at.isoformat() if unit.lease_expires_at else None,
    }


def item_to_unit(item: dict[str, Any]) -> WorkUnit:
    lease = item.get("lease_expires_at")
    return WorkUnit(
        run_id=item["pk"],
        unit_index=int(item["unit_index"]),
        start_index=int(item["start_index"]),
        count=int(item["count"]),
        state=WorkUnitState(item["state"]),
        attempts=int(item.get("attempts", 0)),
        error=item.get("error"),
        updated_at=datetime.fromisoformat(item["updated_at"]),
        lease_expires_at=datetime.fromisoformat(lease) if lease else None,
    )


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _clean(item: dict[str, Any]) -> dict[str, Any]:
    """Drop ``None`` values — DynamoDB stores them as NULL, and reading them back
    as absent keeps the mapping symmetric."""
    return {k: v for k, v in item.items() if v is not None}


class DynamoDbStateStore:
    """A :class:`~docloom.core.state.base.StateStore` backed by one DynamoDB table."""

    scheme = "dynamodb"

    def __init__(
        self,
        table: str,
        *,
        region: str | None = None,
        endpoint_url: str | None = None,
        resource: Any | None = None,
        table_resource: Any | None = None,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
    ) -> None:
        self._lease_seconds = lease_seconds
        self._table_name = table
        if table_resource is not None:
            self._table = table_resource
            return
        if resource is None:
            try:
                import boto3
            except ImportError as exc:
                raise ImportError(
                    "dynamodb:// state needs the AWS extra — pip install 'docloom[aws]'"
                ) from exc
            kwargs: dict[str, Any] = {}
            if region:
                kwargs["region_name"] = region
            if endpoint_url:
                kwargs["endpoint_url"] = endpoint_url
            resource = boto3.resource("dynamodb", **kwargs)
        self._table = resource.Table(table)

    # ── Runs ────────────────────────────────────────────────────────────────
    def create_run(self, run: Run, units: list[WorkUnit]) -> bool:
        """Claim the run id with a conditional put, then write the units.

        ``attribute_not_exists(pk)`` makes exactly one of N simultaneously
        starting workers the planner; the losers get ``False`` rather than
        silently overwriting units the winner has already handed out.

        This inverts the previous units-first ordering. That ordering existed so
        a crash never left a *discoverable* run — but it cannot serialise
        planners, which is the more damaging failure. The marker now goes first
        carrying ``planned=False``, so a half-written plan is discoverable *and*
        distinguishable, and a planner that dies is taken over rather than
        leaving a run that reads as finished.
        """
        marker = _clean(run_to_item(run)) | {"planned": False, "planning_started_at": _now()}
        try:
            self._table.put_item(
                Item=marker, ConditionExpression="attribute_not_exists(pk)"
            )
        except Exception as exc:  # noqa: BLE001 - botocore's typed error needs the client
            if type(exc).__name__ != "ConditionalCheckFailedException":
                raise
            if not self._should_take_over(run.run_id):
                return False
            # The previous planner died mid-plan. Unit writes are keyed by index
            # and byte-identical, so finishing its work is safe.
        # batch_writer chunks at DynamoDB's 25-item limit for us.
        with self._table.batch_writer() as batch:
            for unit in units:
                batch.put_item(Item=_clean(unit_to_item(unit)))
        self._table.update_item(
            Key={"pk": run.run_id, "sk": _RUN_SK},
            UpdateExpression="SET planned = :t, updated_at = :u",
            ExpressionAttributeValues={":t": True, ":u": _now()},
        )
        return True

    def _should_take_over(self, run_id: str) -> bool:
        """True when an unplanned marker has sat long enough to be abandoned."""
        item = self._table.get_item(Key={"pk": run_id, "sk": _RUN_SK}).get("Item")
        if item is None:
            return True                      # vanished between put and read
        if item.get("planned", True):
            return False                     # someone finished the plan
        started = item.get("planning_started_at")
        if not started:
            return True
        age = (datetime.now(UTC) - datetime.fromisoformat(str(started))).total_seconds()
        return age >= PLANNING_TAKEOVER_SECONDS

    def get_run(self, run_id: str) -> Run | None:
        item = self._table.get_item(Key={"pk": run_id, "sk": _RUN_SK}).get("Item")
        return item_to_run(item) if item else None

    def set_run_state(self, run_id: str, state: RunState) -> None:
        self._table.update_item(
            Key={"pk": run_id, "sk": _RUN_SK},
            UpdateExpression="SET #s = :s, updated_at = :u",
            ExpressionAttributeNames={"#s": "state"},
            ExpressionAttributeValues={":s": state.value, ":u": _now()},
        )

    # ── Work units ──────────────────────────────────────────────────────────
    def claim_next_unit(self, run_id: str) -> WorkUnit | None:
        run = self.get_run(run_id)
        # `planned` matters as much as the state: a run whose units are still
        # being written has nothing to claim *yet*, not nothing to claim ever.
        if run is None or not run.planned or run.state in (RunState.PAUSED, RunState.CANCELLED):
            return None
        lease = (datetime.now(UTC) + timedelta(seconds=self._lease_seconds)).isoformat()
        # Walk pending candidates in index order; the conditional update is what
        # makes the claim atomic, so a lost race just advances to the next one.
        for item in self._iter_units(run_id, state=WorkUnitState.PENDING):
            claimed = self._try_claim(run_id, int(item["unit_index"]), lease)
            if claimed is not None:
                return claimed
        return None

    def _try_claim(self, run_id: str, unit_index: int, lease: str) -> WorkUnit | None:
        """Conditionally take one unit. ``None`` if another worker won the race."""
        try:
            response = self._table.update_item(
                Key={"pk": run_id, "sk": unit_sort_key(unit_index)},
                UpdateExpression=(
                    "SET #s = :running, attempts = if_not_exists(attempts, :zero) + :one, "
                    "updated_at = :now, lease_expires_at = :lease"
                ),
                ConditionExpression="#s = :pending",
                ExpressionAttributeNames={"#s": "state"},
                ExpressionAttributeValues={
                    ":running": WorkUnitState.RUNNING.value,
                    ":pending": WorkUnitState.PENDING.value,
                    ":now": _now(),
                    ":lease": lease,
                    ":zero": Decimal(0),
                    ":one": Decimal(1),
                },
                ReturnValues="ALL_NEW",
            )
        except Exception as exc:  # noqa: BLE001 - botocore's typed error needs the client
            if type(exc).__name__ == "ConditionalCheckFailedException":
                return None
            raise
        return item_to_unit(response["Attributes"])

    def complete_unit(self, run_id: str, unit_index: int) -> None:
        self._terminal(run_id, unit_index, WorkUnitState.DONE, error=None)

    def fail_unit(self, run_id: str, unit_index: int, error: str) -> None:
        self._terminal(run_id, unit_index, WorkUnitState.FAILED, error=error)

    def _terminal(
        self, run_id: str, unit_index: int, state: WorkUnitState, *, error: str | None
    ) -> None:
        # Clearing the lease is the point: a terminal unit is held by nobody.
        self._table.update_item(
            Key={"pk": run_id, "sk": unit_sort_key(unit_index)},
            UpdateExpression=(
                "SET #s = :s, updated_at = :u, #e = :e REMOVE lease_expires_at"
            ),
            ExpressionAttributeNames={"#s": "state", "#e": "error"},
            ExpressionAttributeValues={":s": state.value, ":u": _now(), ":e": error},
        )

    def reset_failed_units(self, run_id: str) -> int:
        count = 0
        for item in self._iter_units(run_id, state=WorkUnitState.FAILED):
            if self._requeue(run_id, int(item["unit_index"]), WorkUnitState.FAILED):
                count += 1
        return count

    def reclaim_expired_units(self, run_id: str, *, now: datetime | None = None) -> int:
        cutoff = now or datetime.now(UTC)
        count = 0
        for item in self._iter_units(run_id, state=WorkUnitState.RUNNING):
            lease = item.get("lease_expires_at")
            if not lease or datetime.fromisoformat(lease) > cutoff:
                continue
            if self._requeue(run_id, int(item["unit_index"]), WorkUnitState.RUNNING):
                count += 1
        return count

    def _requeue(self, run_id: str, unit_index: int, expected: WorkUnitState) -> bool:
        """Return a unit to ``pending``, only if it is still in ``expected``.

        Conditional so a concurrent worker that just completed (or reclaimed) the
        unit is never trampled — the same race protection as the claim.
        """
        try:
            self._table.update_item(
                Key={"pk": run_id, "sk": unit_sort_key(unit_index)},
                UpdateExpression=(
                    "SET #s = :pending, updated_at = :u, #e = :null REMOVE lease_expires_at"
                ),
                ConditionExpression="#s = :expected",
                ExpressionAttributeNames={"#s": "state", "#e": "error"},
                ExpressionAttributeValues={
                    ":pending": WorkUnitState.PENDING.value,
                    ":expected": expected.value,
                    ":u": _now(),
                    ":null": None,
                },
            )
        except Exception as exc:  # noqa: BLE001
            if type(exc).__name__ == "ConditionalCheckFailedException":
                return False
            raise
        return True

    # ── Spend rollup ────────────────────────────────────────────────────────
    def add_spend(
        self,
        run_id: str,
        model: str,
        *,
        cost: Decimal,
        calls: int = 1,
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> Decimal:
        """Increment the model row and the run total with ``ADD``.

        ``ADD`` on a DynamoDB number is a server-side atomic increment, and its
        number type is arbitrary-precision decimal, so the counter is exact and
        no read-modify-write race exists. The total row's ``UPDATED_NEW`` gives
        the post-increment value a budget check needs.
        """
        nano = to_nano(cost)
        # The total goes FIRST, deliberately. These are two UpdateItems — each
        # atomic, but not atomic as a pair — so a crash between them leaves the
        # two disagreeing, and the *direction* of that disagreement is a safety
        # property. Total first means a failure over-counts the total relative to
        # the per-model rows, so a budget stops slightly early. Model first would
        # under-count the total, letting a run quietly overshoot its cap, which is
        # the failure a budget exists to prevent. See TODO.md for the fix
        # (TransactWriteItems) and why it was not taken yet.
        total = self._add_spend_row(
            run_id, TOTAL_MODEL, nano, calls, input_tokens, output_tokens
        )
        self._add_spend_row(run_id, model, nano, calls, input_tokens, output_tokens)
        return from_nano(total)

    def _add_spend_row(
        self, run_id: str, model: str, nano: int,
        calls: int, input_tokens: int, output_tokens: int,
    ) -> int:
        response = self._table.update_item(
            Key={"pk": run_id, "sk": spend_sort_key(model)},
            UpdateExpression=(
                "ADD cost_nano :c, calls :n, input_tokens :i, output_tokens :o "
                "SET #m = :model, updated_at = :u"
            ),
            ExpressionAttributeNames={"#m": "model"},
            ExpressionAttributeValues={
                ":c": Decimal(nano), ":n": Decimal(calls),
                ":i": Decimal(input_tokens), ":o": Decimal(output_tokens),
                ":model": model, ":u": _now(),
            },
            ReturnValues="UPDATED_NEW",
        )
        return int(response["Attributes"]["cost_nano"])

    def spend(self, run_id: str) -> list[Spend]:
        from boto3.dynamodb.conditions import Key

        response = self._table.query(
            KeyConditionExpression=(
                Key("pk").eq(run_id) & Key("sk").begins_with(_SPEND_PREFIX)
            )
        )
        return sorted(
            (item_to_spend(item) for item in response.get("Items", [])),
            key=lambda s: s.model,
        )

    def total_spend(self, run_id: str) -> Decimal:
        item = self._table.get_item(
            Key={"pk": run_id, "sk": spend_sort_key(TOTAL_MODEL)}
        ).get("Item")
        return from_nano(int(item["cost_nano"])) if item else Decimal(0)

    def units(self, run_id: str) -> Iterator[WorkUnit]:
        return iter([item_to_unit(item) for item in self._iter_units(run_id)])

    def progress(self, run_id: str) -> dict[WorkUnitState, int]:
        counts = {state: 0 for state in WorkUnitState}
        for item in self._iter_units(run_id):
            counts[WorkUnitState(item["state"])] += 1
        return counts

    def _iter_units(
        self, run_id: str, *, state: WorkUnitState | None = None
    ) -> Iterator[dict[str, Any]]:
        """Every unit item for a run in index order, optionally one state only.

        Pages through the Query — a state filter is applied after the read, so a
        page can come back empty while later pages still hold matches.
        """
        from boto3.dynamodb.conditions import Attr, Key

        kwargs: dict[str, Any] = {
            "KeyConditionExpression": Key("pk").eq(run_id) & Key("sk").begins_with(_UNIT_PREFIX),
            "ScanIndexForward": True,
        }
        if state is not None:
            kwargs["FilterExpression"] = Attr("state").eq(state.value)
        while True:
            response = self._table.query(**kwargs)
            yield from response.get("Items", [])
            last = response.get("LastEvaluatedKey")
            if not last:
                return
            kwargs["ExclusiveStartKey"] = last

    def close(self) -> None:
        pass

    # ── Convenience ─────────────────────────────────────────────────────────
    @staticmethod
    def create_table(resource: Any, table: str) -> Any:
        """Create the single table this store expects (for setup/tests)."""
        return resource.create_table(
            TableName=table,
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"},
                       {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"},
                                  {"AttributeName": "sk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
