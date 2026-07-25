"""Resolving a :class:`~docloom.core.selection.Selection` against the roster.

The core declares a slice's composition; this turns that declaration into the
concrete pools the sampler draws from — a filtered roster, an archetype pool, a
condition set, a wear range.

**Resolution happens once per run, not once per document.** Two reasons, and the
second is the important one:

* It is cheap. Filtering a roster of a few dozen companies for every one of
  25,000 documents is pure waste.
* It is where constraints *fail*. A locale nobody issues from, a company id that
  does not exist, a template that was renamed — all of it surfaces before the
  first document instead of 40 minutes into a run, and for a constraint that
  filters to nothing there is no sensible per-document fallback anyway. Falling
  back to the unfiltered roster is precisely the silent-wrong-corpus failure
  this module was written to end.

**"Use N of them" is seeded from the run id**, so the subset is reproducible: the
same run picks the same ten companies on a resume, on a different worker, or a
year later. Picking them from the process RNG would make a resumed unit draw a
different pool and quietly change the corpus mid-run.
"""

from __future__ import annotations

from dataclasses import dataclass
from random import Random

from docloom.core.enums import DocumentCondition
from docloom.core.pipeline.source import stable_seed
from docloom.core.selection import Selection, UnsupportedConstraint
from docloom.packs.invoice.catalog import (
    ALL_ARCHETYPES,
    GENERAL_ARCHETYPES,
    HANDWRITTEN_ARCHETYPE,
    TELECOM_ARCHETYPE,
    Catalogue,
    CompanyRoster,
)
from docloom.packs.invoice.enums import (
    DIGITAL_NATIVE_BUSINESS_TYPES,
    EARLIEST_ISSUE_DATE,
    GOODS_BUSINESS_TYPES,
    BusinessType,
)

#: Index the subset RNG is seeded at. Negative so it can never collide with a
#: document index, which is what keeps the subset independent of any document.
_SUBSET_INDEX = -1

#: Wear when a slice does not ask for one — the well-used look every document
#: had before wear was selectable. Unchanged on purpose: a run that says nothing
#: about wear must generate exactly what it generated before.
DEFAULT_WEAR: tuple[float, float] = (1.0, 1.0)


@dataclass(frozen=True, slots=True)
class Composition:
    """The pools one run draws from, resolved and validated."""

    roster: CompanyRoster
    #: Empty means "each company's own template", the unconstrained default.
    archetypes: tuple[str, ...]
    conditions: tuple[DocumentCondition, ...]
    wear: tuple[float, float]
    goods_receipt: bool

    def archetype_for(self, rng: Random, default: str) -> str:
        """The template for one document: a constrained pick, or the company's own."""
        return rng.choice(self.archetypes) if self.archetypes else default

    def condition_for(self, rng: Random) -> DocumentCondition:
        return rng.choice(self.conditions)

    def wear_for(self, rng: Random) -> float:
        low, high = self.wear
        return 1.0 if low == high == 1.0 else round(rng.uniform(low, high), 3)


def _subset(items: list, count: int, run_id: str, what: str) -> list:
    """``count`` of ``items``, chosen reproducibly from the run id."""
    if count > len(items):
        raise UnsupportedConstraint(
            f"asked for {count} {what} but only {len(items)} match the other "
            f"constraints on this slice"
        )
    rng = Random(stable_seed(run_id, _SUBSET_INDEX))
    return rng.sample(items, count)


def resolve(selection: Selection, catalogue: Catalogue, run_id: str) -> Composition:
    """Turn a declared selection into concrete pools, or raise.

    Every filter narrows the same company list, so the constraints compose:
    ``locales: [fr-FR]`` plus ``business_types: [telecom]`` means French telecom
    issuers, and if the roster has none, that is an error rather than a silent
    fallback to everybody.
    """
    companies = list(catalogue.roster().companies)

    if selection.companies:
        known = {c.company_id for c in companies}
        missing = sorted(set(selection.companies) - known)
        if missing:
            raise UnsupportedConstraint(
                f"no such company: {', '.join(missing)}", field="companies",
                known=tuple(sorted(known)),
            )
        companies = [c for c in companies if c.company_id in set(selection.companies)]

    if selection.locales:
        wanted = set(selection.locales)
        companies = [c for c in companies if str(c.locale) in wanted]
        if not companies:
            raise UnsupportedConstraint(
                f"no company issues in {', '.join(sorted(wanted))}", field="locales",
                known=tuple({str(c.locale) for c in catalogue.roster().companies}),
            )

    if selection.business_types:
        try:
            wanted_types = {BusinessType(b) for b in selection.business_types}
        except ValueError as exc:
            raise UnsupportedConstraint(
                str(exc), field="business_types",
                known=tuple(b.value for b in BusinessType),
            ) from exc
        companies = [c for c in companies if c.business_type in wanted_types]
        if not companies:
            raise UnsupportedConstraint(
                "no company in this slice has business type "
                f"{', '.join(sorted(b.value for b in wanted_types))}"
            )

    if selection.goods_receipt:
        # A receiver signs for things that were delivered, so the issuer has to
        # be a goods seller. Filtering here rather than by rejection sampling
        # makes "goods receipt from one named services company" an error instead
        # of a 200-try loop that quietly gives up.
        companies = [c for c in companies if c.business_type in GOODS_BUSINESS_TYPES]
        if not companies:
            raise UnsupportedConstraint(
                "a goods receipt needs a company that sells physical goods, and "
                "none in this slice does"
            )

    conditions = selection.effective_conditions
    if DocumentCondition.HANDWRITTEN in conditions:
        # Nobody hand-writes a 400-line itemised telecom bill onto a pad. The
        # telecom sampler emits 60–400 call-detail lines, and the hand-filled
        # archetype draws one ruled row per line, so a telecom issuer here would
        # produce a fifty-page "handwritten" invoice — expensive to render and
        # absurd on its face.
        #
        # Filtered rather than rejected, matching the goods-receipt rule above:
        # this is a fact about the physical document, not a mistake the operator
        # made. Pin `business_types: [telecom]` *and* handwritten and you get the
        # error below, because then it really is contradictory.
        without_telecom = [c for c in companies if c.business_type is not BusinessType.TELECOM]
        if not without_telecom:
            raise UnsupportedConstraint(
                "a handwritten document is filled in by hand on a ruled pad, so it "
                "cannot be an itemised telecom bill — this slice has no other issuer"
            )
        companies = without_telecom

    if any(c is not DocumentCondition.CLEAN for c in conditions):
        # Born-digital issuers (software, AI platforms) only ever produce clean
        # digital PDFs — too new to have a hand-filled pad, and no paper original
        # old enough to survive as a degraded scan. So they are dropped from any
        # non-CLEAN slice, handwritten and light/heavy scan alike. Filtered for the
        # whole slice like the rules above; runs keep one condition per slice, so
        # the corner where a single slice mixes CLEAN with a scan (which would also
        # drop them from the clean draws) is not how slices are written. Telecom's
        # handwritten exclusion above is a separate rule (line count, not age).
        grounded = [c for c in companies
                    if c.business_type not in DIGITAL_NATIVE_BUSINESS_TYPES]
        if not grounded:
            raise UnsupportedConstraint(
                "a handwritten or scanned document needs an issuer old enough to "
                "have paper invoices; every company in this slice is born-digital "
                "(software / AI platform)"
            )
        companies = grounded

    if selection.issue_date_range is not None:
        # A business cannot issue an invoice before it existed. If this slice's
        # whole date window predates a type's era (an AI platform billing usage in
        # 2019), that type can't appear — drop it, mirroring the born-digital
        # condition filter above. Types whose era falls inside the window stay, and
        # the sampler floors their issue date to the era.
        window_end = selection.issue_date_range[1]
        current = [c for c in companies
                   if (f := EARLIEST_ISSUE_DATE.get(c.business_type)) is None
                   or f <= window_end]
        if not current:
            raise UnsupportedConstraint(
                "this slice's date window ends before every eligible issuer's era "
                "existed (e.g. an AI-platform vendor has no invoices that old)"
            )
        companies = current

    if selection.company_count is not None:
        companies = _subset(companies, selection.company_count, run_id, "companies")

    if not companies:
        raise UnsupportedConstraint("no company matches this slice's constraints")

    archetypes = _resolve_archetypes(selection, run_id)
    if DocumentCondition.HANDWRITTEN in conditions and archetypes:
        # The pad is the document, so a handwritten slice cannot also be pinned
        # to a typeset layout. Saying so beats rendering one and ignoring the
        # other.
        typeset = [a for a in archetypes if a != HANDWRITTEN_ARCHETYPE]
        if typeset:
            raise UnsupportedConstraint(
                f"a handwritten slice always renders as {HANDWRITTEN_ARCHETYPE}, so it "
                f"cannot also use {', '.join(typeset)}"
            )

    return Composition(
        roster=CompanyRoster(companies),
        archetypes=archetypes,
        conditions=conditions,
        wear=selection.wear or DEFAULT_WEAR,
        goods_receipt=selection.goods_receipt,
    )


def _resolve_archetypes(selection: Selection, run_id: str) -> tuple[str, ...]:
    if selection.archetypes:
        unknown = sorted(set(selection.archetypes) - set(ALL_ARCHETYPES))
        if unknown:
            raise UnsupportedConstraint(
                f"no such template: {', '.join(unknown)}", field="archetypes",
                known=ALL_ARCHETYPES,
            )
        return tuple(selection.archetypes)
    if selection.archetype_count is not None:
        # Drawn from the general layouts only: the telecom and handwritten
        # templates are routed to by content, not chosen as looks, so including
        # them in "use 3 templates" would produce telecom pages for companies
        # that sell paint.
        return tuple(_subset(list(GENERAL_ARCHETYPES), selection.archetype_count,
                             run_id, "templates"))
    return ()
