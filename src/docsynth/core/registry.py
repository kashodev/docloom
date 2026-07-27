"""Pack discovery.

Two ways a pack becomes available:

1. **Built-in** — registered on import by ``docsynth.packs``.
2. **Third-party** — declared as a ``docsynth.packs`` entry point, so
   ``pip install docsynth-contract`` makes ``document.pack: contract`` work with
   no change to docsynth itself.

Entry points are the reason this indirection exists at all. Without them an
out-of-tree pack would need a patch to a hard-coded dict, which is the
difference between an extensible project and one people fork.
"""

from __future__ import annotations

from importlib.metadata import entry_points

from docsynth.core.pack import DocumentPack

ENTRY_POINT_GROUP = "docsynth.packs"

_REGISTRY: dict[str, DocumentPack] = {}
_ENTRY_POINTS_LOADED = False


def register_pack(pack: DocumentPack) -> None:
    """Register a pack under its ``name``.

    Two *different* packs claiming one name is an error — silent shadowing would
    make a mis-installed plugin very hard to diagnose. Two instances of the same
    pack class is not: a built-in pack is registered on import by
    ``docsynth.packs`` **and** discovered through its entry point, so an installed
    copy legitimately registers the same pack twice, from two code paths that do
    not know about each other.

    Comparing by identity treated that as a conflict, which broke every
    installed copy of docsynth — ``available_packs()`` raised before returning
    anything. It survived because the test suite and local development both run
    from a source checkout, where no entry points are declared and the second
    registration never happens.
    """
    existing = _REGISTRY.get(pack.name)
    if existing is not None and type(existing) is not type(pack):
        raise ValueError(
            f"a different pack is already registered as {pack.name!r}: "
            f"{type(existing).__module__}.{type(existing).__qualname__} vs "
            f"{type(pack).__module__}.{type(pack).__qualname__}"
        )
    _REGISTRY[pack.name] = pack


def _load_entry_points() -> None:
    global _ENTRY_POINTS_LOADED
    if _ENTRY_POINTS_LOADED:
        return
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        register_pack(ep.load()())
    _ENTRY_POINTS_LOADED = True


def get_pack(name: str) -> DocumentPack:
    _load_entry_points()
    try:
        return _REGISTRY[name]
    except KeyError:
        available = ", ".join(sorted(_REGISTRY)) or "none"
        raise KeyError(f"no document pack named {name!r} (available: {available})") from None


def available_packs() -> tuple[str, ...]:
    _load_entry_points()
    return tuple(sorted(_REGISTRY))
