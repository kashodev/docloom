"""Built-in document packs.

Importing this module registers the packs that ship with docsynth. Third-party
packs register themselves through the ``docsynth.packs`` entry-point group — see
``docsynth.core.registry``.
"""

from docsynth.core.registry import register_pack
from docsynth.packs.invoice import InvoicePack

register_pack(InvoicePack())

__all__ = ["InvoicePack"]
