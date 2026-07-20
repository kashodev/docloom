"""Built-in document packs.

Importing this module registers the packs that ship with docloom. Third-party
packs register themselves through the ``docloom.packs`` entry-point group — see
``docloom.core.registry``.
"""

from docloom.core.registry import register_pack
from docloom.packs.invoice import InvoicePack

register_pack(InvoicePack())

__all__ = ["InvoicePack"]
