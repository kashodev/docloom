"""The prompt layer — arrow-key selects when possible, a plain fallback always.

``questionary`` (optional, ``pip install 'docloom[studio]'``) gives the arrow-key
menus in the mockups. When it is absent, :class:`FallbackPrompter` asks the same
questions with numbered menus and plain input, so the wizard still runs anywhere.
Both live behind one :class:`Prompter` protocol, so the wizard never knows which
it got — and a :class:`ScriptedPrompter` drives it deterministically in tests.

A prompt is only ever reached for a value a flag did not already supply, so a
fully-flagged invocation touches none of this (and needs no TTY).
"""
from __future__ import annotations

import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from docloom.studio.types import StudioError


@dataclass(frozen=True)
class Choice:
    """One selectable option: the ``value`` returned, a ``label`` shown, an
    optional dim ``hint`` beside it."""

    value: str
    label: str
    hint: str = ""


class Prompter(Protocol):
    def select(self, message: str, choices: Sequence[Choice], *, default: str = "") -> str: ...
    def text(self, message: str, *, default: str = "") -> str: ...
    def confirm(self, message: str, *, default: bool = False) -> bool: ...


def _abort_if_cancelled(value: object) -> object:
    """questionary returns ``None`` on Ctrl-C/EOF — turn that into a clean exit."""
    if value is None:
        raise StudioError("cancelled")
    return value


class QuestionaryPrompter:
    """Arrow-key prompts via ``questionary`` (imported lazily so it stays optional)."""

    def __init__(self) -> None:
        import questionary
        self._q = questionary

    def select(self, message: str, choices: Sequence[Choice], *, default: str = "") -> str:
        opts = [self._q.Choice(title=c.label + (f"   {c.hint}" if c.hint else ""), value=c.value)
                for c in choices]
        default_opt = next(
            (o for o, c in zip(opts, choices, strict=True) if c.value == default), None)
        return _abort_if_cancelled(self._q.select(message, choices=opts, default=default_opt).ask())

    def text(self, message: str, *, default: str = "") -> str:
        return _abort_if_cancelled(self._q.text(message, default=default).ask())

    def confirm(self, message: str, *, default: bool = False) -> bool:
        return _abort_if_cancelled(self._q.confirm(message, default=default).ask())


class FallbackPrompter:
    """Numbered menus + plain input — no dependency, works over any TTY."""

    def select(self, message: str, choices: Sequence[Choice], *, default: str = "") -> str:
        print(message)
        for i, c in enumerate(choices, 1):
            mark = ">" if c.value == default else " "
            print(f"  {mark} {i}. {c.label}" + (f"   {c.hint}" if c.hint else ""))
        while True:
            raw = input(f"  choose 1-{len(choices)}"
                        + (f" [{default}]: " if default else ": ")).strip()
            if not raw and default:
                return default
            if raw.isdigit() and 1 <= int(raw) <= len(choices):
                return choices[int(raw) - 1].value
            print("  not a valid choice")

    def text(self, message: str, *, default: str = "") -> str:
        raw = input(f"{message}" + (f" [{default}]: " if default else ": ")).strip()
        return raw or default

    def confirm(self, message: str, *, default: bool = False) -> bool:
        raw = input(f"{message} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
        return default if not raw else raw in ("y", "yes")


class ScriptedPrompter:
    """Test double — pops queued answers in order and records what was asked."""

    def __init__(self, answers: Sequence[object]) -> None:
        self._answers = list(answers)
        self.asked: list[str] = []

    def _next(self, message: str) -> object:
        self.asked.append(message)
        if not self._answers:
            raise AssertionError(f"ScriptedPrompter ran out of answers at {message!r}")
        return self._answers.pop(0)

    def select(self, message: str, choices: Sequence[Choice], *, default: str = "") -> str:
        return str(self._next(message))

    def text(self, message: str, *, default: str = "") -> str:
        value = self._next(message)
        return default if value == "" else str(value)

    def confirm(self, message: str, *, default: bool = False) -> bool:
        return bool(self._next(message))


def is_interactive() -> bool:
    """True only when we can actually prompt — both ends are a terminal."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def get_prompter() -> Prompter:
    """The best prompter available: arrow-key if ``questionary`` is installed,
    else the plain fallback."""
    try:
        return QuestionaryPrompter()
    except ImportError:
        return FallbackPrompter()
