"""A spinner for synchronous (local) work, plus a stdin drain.

Runs a callable on a worker thread and animates a spinner on stderr until it
finishes — optionally with an ``x/total`` status polled from ``progress`` — so an
operator sees the studio is busy rather than a frozen prompt. No dependency: an
ASCII spinner over ``\\r``. When stderr is not a TTY (piped, captured, CI) it just
calls the function, leaving output clean.

``drain_stdin`` discards anything typed while work was running, so keystrokes
pressed during a captured run don't leak into the next menu (which otherwise reads
a stray Enter as "accept the default").
"""
from __future__ import annotations

import itertools
import sys
import threading
import time
from collections.abc import Callable


def drain_stdin() -> None:
    """Discard buffered terminal input (best effort; no-op off a Unix TTY)."""
    try:
        import termios
        if sys.stdin.isatty():
            termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
    except Exception:
        pass


def run_with_spinner[T](message: str, fn: Callable[[], T], *,
                        progress: Callable[[], str] | None = None) -> T:
    """Call ``fn`` while spinning; return its result (exceptions propagate).

    ``progress`` (optional) is polled ~2x/s for a short ``x/total`` status shown
    after the message. Any error it raises is swallowed — progress is cosmetic.
    """
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
    status, last_poll = "", 0.0
    for frame in itertools.cycle("|/-\\"):
        thread.join(0.1)
        if not thread.is_alive():
            break
        if progress is not None and (now := time.monotonic()) - last_poll > 0.5:
            last_poll = now
            try:
                status = progress() or ""
            except Exception:
                status = ""
        line = f"  {frame} {message}" + (f"  {status}" if status else "")
        sys.stderr.write("\r" + line + "   ")
        sys.stderr.flush()
    sys.stderr.write("\r" + " " * (len(message) + len(status) + 10) + "\r")  # wipe the line
    sys.stderr.flush()

    if "error" in err:
        raise err["error"]
    return box["value"]
