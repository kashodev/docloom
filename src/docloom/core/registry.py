"""Pack discovery.

Two ways a pack becomes available:

1. **Built-in** — registered on import by ``docloom.packs``.
2. **Third-party** — declared as a ``docloom.packs`` entry point, so
   ``pip install docloom-contract`` makes ``document.pack: contract`` work with
   no change to docloom itself.

Entry points are the reason this indirection exists at all. Without them an
out-of-tree pack would need a patch to a hard-coded dict, which is the
difference between an extensible project and one people fork.
"""

from __future__ import annotations

from importlib.metadata import entry_points

from docloom.core.pack import DocumentPack

ENTRY_POINT_GROUP = "docloom.packs"

_REGISTRY: dict[str, DocumentPack] = {}
_ENTRY_POINTS_LOADED = False


def register_pack(pack: DocumentPack) -> None:
    """Register a pack under its ``name``. Re-registering the same name is an
    error — silent shadowing would make a mis-installed plugin very hard to
    diagnose."""
    if pack.name in _REGISTRY and _REGISTRY[pack.name] is not pack:
        raise ValueError(f"a different pack is already registered as {pack.name!r}")
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
