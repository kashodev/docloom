"""docloom — weave templates and generated data into realistic documents.

Two threads go in: an archetype (structure) and a golden record (computed
ground truth). One artefact comes out, alongside a golden dataset you can score
an extraction pipeline against.

The kernel (:mod:`docloom.core`) is document-type agnostic. Each document type
lives in a pack (:mod:`docloom.packs`) that supplies its record shape,
templates, and vocabulary.
"""

from docloom.core import DocumentPack, GoldenRecord, available_packs, get_pack

__version__ = "0.1.0"

__all__ = ["DocumentPack", "GoldenRecord", "available_packs", "get_pack", "__version__"]
