"""Structured logging tests.

Two things are worth pinning: the module renders into console *and* the
Cloud-Logging JSON shape from the same call sites; and the events this project
most needs — a completed unit with its context, a *swallowed* unit failure, an
empty completion — are actually emitted, since a silent handled-exception is
exactly what has bitten this codebase before.

The assertions read the **rendered JSON** rather than `structlog.testing.
capture_logs`, deliberately: `capture_logs` bypasses the processor chain, so it
would not exercise the contextvars merge (bound `run_id` / `unit`) or the
Cloud-Logging field mapping — the two things most worth testing.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

import docloom.packs  # noqa: F401 - registers the invoice pack
from docloom.core import get_pack
from docloom.core.logging import bound, configure, get_logger
from docloom.core.pipeline import HtmlRenderer, create_run, work_run
from docloom.core.pipeline.renderer import RenderedDocument
from docloom.core.state.sqlite import SqliteStateStore
from docloom.core.storage.local import LocalBlobStore


@pytest.fixture
def json_logs() -> Callable[[], list[dict]]:
    """Configure JSON+DEBUG logging into a StringIO we own, and return a reader
    for the events emitted so far. A StringIO rather than capsys because
    structlog's PrintLogger binds the stream at configure time, and pytest's
    capsys stream does not survive that reliably."""
    import io
    buf = io.StringIO()
    configure(level="debug", fmt="json", stream=buf, force=True)

    def read() -> list[dict]:
        return [json.loads(line) for line in buf.getvalue().strip().splitlines()
                if line.strip().startswith("{")]

    yield read
    configure(force=True)   # restore defaults for other tests


# ── Rendering: console vs Cloud Logging JSON ────────────────────────────────
def test_json_mode_emits_cloud_logging_fields(json_logs) -> None:  # noqa: ANN001
    get_logger("t").warning("unit failed", unit=4, error="boom")
    record = json_logs()[-1]
    # Cloud Logging keys off `severity` and `message`, not structlog's defaults.
    assert record["severity"] == "WARNING"
    assert record["message"] == "unit failed"
    assert record["unit"] == 4 and record["error"] == "boom"


def test_console_mode_is_not_json() -> None:
    import io
    buf = io.StringIO()
    configure(fmt="console", stream=buf, force=True)
    get_logger("t").info("hello", key="val")
    err = buf.getvalue()
    # Console output is ANSI-colourised, so key and value are not a contiguous
    # substring — check they appear and that the line is not JSON.
    assert "hello" in err and "key" in err and "val" in err
    assert not err.strip().startswith("{")
    configure(force=True)


def test_level_filtering_hides_below_threshold() -> None:
    import io
    buf = io.StringIO()
    configure(level="warning", fmt="json", stream=buf, force=True)
    log = get_logger("t")
    log.info("hidden")
    log.warning("shown")
    events = {json.loads(line)["message"]
              for line in buf.getvalue().strip().splitlines()}
    assert "shown" in events and "hidden" not in events
    configure(force=True)


# ── Context binding correlates a worker's lines ─────────────────────────────
def test_bound_context_rides_every_line(json_logs) -> None:  # noqa: ANN001
    with bound(run_id="r1", task=2):
        get_logger("a").info("one")
        get_logger("b").warning("two")
    events = json_logs()
    assert all(e["run_id"] == "r1" and e["task"] == 2 for e in events)
    assert {e["message"] for e in events} == {"one", "two"}


def test_bound_context_is_scoped(json_logs) -> None:  # noqa: ANN001
    with bound(unit=3):
        get_logger("a").info("inside")
    get_logger("a").info("outside")
    events = {e["message"]: e for e in json_logs()}
    assert events["inside"]["unit"] == 3
    assert "unit" not in events["outside"]


# ── The events that matter get logged ───────────────────────────────────────
def test_a_completed_unit_is_logged_with_its_context(tmp_path: Path, json_logs) -> None:  # noqa: ANN001
    blob = LocalBlobStore(str(tmp_path / "b"))
    state = SqliteStateStore(tmp_path / "s.db")
    source = get_pack("invoice").default_source(max_line_items=4)
    create_run(state, run_id="r", pack="invoice", config_id="c", total=6, unit_size=3)

    work_run(state, run_id="r", source=source,
             renderer=HtmlRenderer(get_pack("invoice")), blob=blob)

    events = json_logs()
    completed = [e for e in events if e["message"] == "unit completed"]
    assert len(completed) == 2                       # 6 docs / unit_size 3
    assert all(e["run_id"] == "r" for e in completed)   # bound context rides along
    assert all("unit" in e and e["documents"] == 3 for e in completed)
    assert any(e["message"] == "run completed" for e in events)


class _Boom(HtmlRenderer):
    def render(self, record) -> RenderedDocument:  # noqa: ANN001
        raise RuntimeError("render exploded")


def test_a_swallowed_unit_failure_is_still_logged(tmp_path: Path, json_logs) -> None:  # noqa: ANN001
    """The discipline that matters most here: a failure that is caught and
    handled (unit marked failed, worker continues) must not be silent — that is
    the exact shape of several bugs this project has hit."""
    blob = LocalBlobStore(str(tmp_path / "b"))
    state = SqliteStateStore(tmp_path / "s.db")
    source = get_pack("invoice").default_source(max_line_items=4)
    create_run(state, run_id="r", pack="invoice", config_id="c", total=6, unit_size=3)

    work_run(state, run_id="r", source=source, renderer=_Boom(get_pack("invoice")),
             blob=blob)

    events = json_logs()
    failures = [e for e in events if e["message"] == "unit failed"]
    assert len(failures) == 2
    assert all("render exploded" in e["error"] for e in failures)
    assert all(e["severity"] == "WARNING" for e in failures)
    assert any(e["message"] == "run has failed units — a re-run is needed" for e in events)


def test_an_empty_completion_is_logged(json_logs) -> None:  # noqa: ANN001
    """The DeepSeek case — logged at WARNING as it happens, not only surfaced in
    the final report."""
    import asyncio
    from decimal import Decimal as D

    from docloom.core.providers.base import CompletionRequest, CompletionResult, Usage
    from docloom.core.providers.catalogue_runner import CatalogueItem, CatalogueRunner
    from docloom.core.providers.mix import ProviderMix
    from docloom.core.providers.pricing import pricing_for

    class Empty:
        name = "deepseek"; model = "deepseek-v4-flash"; pricing = pricing_for("__local__")
        async def complete(self, request):  # noqa: ANN001
            return CompletionResult("", Usage(37, 48), self.model, self.name, D("0.001"))
        def estimate_cost(self, request):  # noqa: ANN001
            return D("0.001")

    mix = ProviderMix([Empty()], [1.0])
    items = [CatalogueItem("i0", CompletionRequest(system="s", prompt="p"))]
    asyncio.run(CatalogueRunner(mix).run(items))

    empties = [e for e in json_logs() if e["message"] == "empty completion"]
    assert empties and empties[0]["provider"] == "deepseek"
    assert empties[0]["severity"] == "WARNING"


def test_a_failure_record_leads_with_severity_and_message() -> None:
    """The two fields that say *what happened* and *how bad* must come first.

    Renaming level/event in place appended them after the structured traceback,
    so anything that truncated a record dropped exactly the fields a search keys
    on — which is how a whole sharded build failed with nothing legible in Cloud
    Logging.
    """
    import io
    import json

    buf = io.StringIO()
    configure(level="info", fmt="json", stream=buf, force=True)
    try:
        raise RuntimeError("provider is out of credit")
    except RuntimeError as exc:
        get_logger("t").warning("catalogue unit failed", error=repr(exc), exc_info=exc)

    line = buf.getvalue().strip()
    assert list(json.loads(line))[:2] == ["severity", "message"]
    record = json.loads(line)
    assert record["severity"] == "WARNING"
    assert record["message"] == "catalogue unit failed"


def test_a_traceback_carries_no_frame_locals() -> None:
    """``dict_tracebacks`` serialises every local in every frame. A catalogue
    unit holds the company rows and 30,000 products, so the one record that
    explains a failure becomes unusably large. Keep the frames, drop the locals.
    """
    import io
    import json

    buf = io.StringIO()
    configure(level="info", fmt="json", stream=buf, force=True)

    def fails() -> None:
        products = [{"sku": i, "description": "x" * 80} for i in range(5000)]  # noqa: F841
        raise RuntimeError("boom")

    try:
        fails()
    except RuntimeError as exc:
        get_logger("t").warning("catalogue unit failed", exc_info=exc)

    line = buf.getvalue().strip()
    record = json.loads(line)
    frames = record["exception"][0]["frames"]
    assert frames, "the traceback itself must survive"
    assert all("locals" not in f for f in frames)
    assert len(line) < 4000, f"failure record is {len(line)} bytes; locals leaked back in"


def test_a_re_run_says_already_planned_not_another_worker(json_logs, tmp_path) -> None:  # noqa: ANN001
    """create_run on an already-planned run (a re-run, or a retried single task)
    must not log 'another worker is planning' — there may be no other worker at
    all. It should say the run is already planned and resume it."""
    from docloom.core.state import SqliteStateStore

    state = SqliteStateStore(tmp_path / "s.db")
    kw = dict(run_id="r", pack="invoice", config_id="c", total=6, unit_size=3)
    create_run(state, **kw)                    # first call plans it
    json_logs()                                # drain
    create_run(state, **kw)                    # second call: already planned

    msgs = [e["message"] for e in json_logs()]
    assert "run already planned; resuming it" in msgs
    assert "another worker is planning this run; waiting" not in msgs
