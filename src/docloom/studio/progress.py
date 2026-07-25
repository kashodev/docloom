"""A tiny spinner for synchronous (local) work.

Runs a callable on a worker thread and animates a spinner on stderr until it
finishes, so an operator sees the studio is busy rather than a frozen prompt. No
dependency — an ASCII spinner over ``\\r``. When stderr is not a TTY (piped,
captured, CI) it just calls the function, leaving output clean.
"""
from __future__ import annotations

import itertools
import sys
import threading
from collections.abc import Callable


def run_with_spinner[T](message: str, fn: Callable[[], T]) -> T:
    """Call ``fn`` while spinning; return its result (exceptions propagate)."""
    if not sys.stderr.isatty():
        return fn()

    box: dict[str, T] = {}
    err: dict[str, BaseException] = {}

    def worker() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:
            err["error"] = exc

    thread = threading.Thread(target=worker)
    thread.start()
    for frame in itertools.cycle("|/-\\"):
        thread.join(0.1)
        if not thread.is_alive():
            break
        sys.stderr.write(f"\r  {frame} {message} ")
        sys.stderr.flush()
    sys.stderr.write("\r" + " " * (len(message) + 6) + "\r")  # wipe the spinner line
    sys.stderr.flush()

    if "error" in err:
        raise err["error"]
    return box["value"]
