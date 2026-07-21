"""Run state: one protocol, backends chosen by URI scheme.

    sqlite:///path/to/runs.db     SQLite (default; no server)
    ./runs.db  or  runs.db        shorthand for sqlite://
    firestore://project/database  Firestore  (pip install 'docloom[gcp]')
    dynamodb://table              DynamoDB   (pip install 'docloom[aws]')

The cloud backends are imported lazily so the default path needs no cloud SDK.
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlparse

from docloom.core.state.base import Run, StateStore, WorkUnit
from docloom.core.state.sqlite import SqliteStateStore

__all__ = ["Run", "StateStore", "SqliteStateStore", "WorkUnit", "open_state"]


def open_state(uri: str) -> StateStore:
    """Construct the state store named by ``uri``."""
    parsed = urlparse(uri)
    scheme = parsed.scheme or "sqlite"

    if scheme == "sqlite":
        # sqlite:///abs/runs.db -> /abs/runs.db ; bare runs.db -> runs.db
        return SqliteStateStore(parsed.path if parsed.scheme else uri)

    if scheme == "firestore":
        try:
            from docloom.core.state.firestore import FirestoreStateStore
        except ImportError as exc:
            raise ImportError(
                "firestore:// state needs the GCP extra — pip install 'docloom[gcp]'"
            ) from exc
        return FirestoreStateStore(project=parsed.netloc, database=parsed.path.lstrip("/"))

    if scheme == "dynamodb":
        # dynamodb://table  ·  dynamodb://table?region=eu-west-1&endpoint_url=…
        from docloom.core.state.dynamodb import DynamoDbStateStore

        options = dict(parse_qsl(parsed.query))
        return DynamoDbStateStore(
            parsed.netloc,
            region=options.get("region"),
            endpoint_url=options.get("endpoint_url"),
        )

    raise ValueError(f"unsupported state scheme {scheme!r} in {uri!r}")
