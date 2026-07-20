"""Kernel vocabularies — meaningful for any document type.

Anything here must make sense for an invoice, a contract, a delivery note, and
a court filing alike. Document-specific vocabularies (billing models, clause
types, line-item kinds) belong to their pack.

``Jurisdiction`` is the borderline case worth explaining: the *enum* is
kernel-level because a jurisdiction is a legal identity that any document type
may carry, but the *profile* attached to it is pack-specific. An invoice pack
hangs tax rules off ``Jurisdiction.FR``; a contract pack would hang governing
law and enforceability off the same member.
"""

from __future__ import annotations

from enum import StrEnum


class Jurisdiction(StrEnum):
    """Legal jurisdiction the document is issued under.

    Canada is split by province because downstream profiles differ materially
    there — harmonised HST vs separate GST+PST vs GST+QST for invoices, and
    distinct civil/common law regimes for contracts.
    """

    US = "US"
    CA_ON = "CA-ON"
    CA_QC = "CA-QC"
    CA_BC = "CA-BC"
    CA_AB = "CA-AB"
    GB = "GB"
    FR = "FR"


class DocumentCondition(StrEnum):
    """Capture quality of the rendered artefact.

    Applies to any document type: a scanned contract degrades exactly like a
    scanned invoice, and the extraction pipeline has to cope with both.
    """

    CLEAN = "clean"                # digital PDF, text layer intact
    LIGHT_SCAN = "light_scan"      # mild skew/noise, text layer removed
    HEAVY_SCAN = "heavy_scan"      # pronounced degradation
    HANDWRITTEN = "handwritten"    # handwriting fonts + jitter, then degraded


class RunState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    FAILED = "failed"


class WorkUnitState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
