"""Run composition — what a slice of a run is allowed to draw from.

A run's *size* is the work unit's business (see
:mod:`docloom.core.pipeline.planner`). Its *composition* is this module's: 10,000
invoices from one company on one template, 2,500 in French, 1,250 handwritten and
well preserved. Those are declarations about a population, not about any one
document, which is why they live beside the run rather than inside a record.

**This is a declaration, not an interpretation.** The kernel carries a
:class:`Selection` to the pack's source and no further — it has no idea what a
"company" is, and `archetypes` are template names it never resolves. Each pack
decides what it can honour and what it must reject, because only the pack knows
its own roster. Fields here are therefore the *vocabulary* of a slice, chosen to
match one-for-one what ``deploy/gcp/run.example.yaml`` already lets an operator
write — the format existed before it could be executed, and inventing a second
one to execute it would have been the wrong repair.

Two things are deliberately not here:

* **Weights.** ``conditions`` is a set drawn from uniformly, not a distribution.
  A weighted mix is expressible today by splitting into two slices with
  different counts, which also gets you separate run ids and separate resume —
  strictly more useful than a weight.
* **Anything per-document.** A selection constrains a population. Which company
  document 4,182 actually drew is a fact about that document, and it is on the
  golden row.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Any

from docloom.core.enums import DocumentCondition

#: Wear range meaning "well preserved" — the crisp variant. Kept in step with
#: ``packs.invoice.record._CRISP_WEAR``, the threshold ``is_crisp`` is derived
#: from, so a slice asking for crisp documents produces rows that say so.
CRISP_WEAR: tuple[float, float] = (0.0, 0.25)

#: Wear range meaning "however it turned out" — the full spread, which is what
#: "random degradation" asks for. Not the same as the unconstrained default:
#: leaving ``wear`` unset pins every document at 1.0, the well-used look.
VARIED_WEAR: tuple[float, float] = (0.35, 1.0)

_NAMED_WEAR = {"crisp": CRISP_WEAR, "varied": VARIED_WEAR, "worn": (1.0, 1.0)}


def _names(value: Any, field_name: str) -> tuple[str, ...]:
    """A constraint written as a list, a bare scalar, or ``all``."""
    if value is None or value == "all":
        return ()
    if isinstance(value, bool | int):
        raise ValueError(f"`{field_name}` takes names or `all`, not {value!r}")
    if isinstance(value, str):
        return (value,)
    return tuple(str(v) for v in value)


def _count(value: Any, field_name: str) -> int | None:
    """The "use N of them" form — an integer where a list would also be legal."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 1:
        raise ValueError(f"`{field_name}` as a count must be at least 1, got {value}")
    return value


def _wear(value: Any) -> tuple[float, float] | None:
    if value is None:
        return None
    if isinstance(value, str):
        if value not in _NAMED_WEAR:
            raise ValueError(
                f"unknown wear {value!r} (known: {', '.join(sorted(_NAMED_WEAR))}, "
                "or a number, or a [low, high] range)"
            )
        return _NAMED_WEAR[value]
    if isinstance(value, int | float):
        return (float(value), float(value))
    low, high = (float(v) for v in value)
    if low > high:
        raise ValueError(f"wear range is [low, high]; got [{low}, {high}]")
    if not (low >= 0.0 and high <= 1.0):
        raise ValueError(f"wear must lie in [0, 1]; got [{low}, {high}]")
    return (low, high)


def _as_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):        # YAML parses a bare `2026-01-01` to a date
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError(f"invalid date {value!r}; use YYYY-MM-DD") from exc


def _date_range(value: Any) -> tuple[date, date] | None:
    """A ``[from, to]`` issue-date range — two ISO dates, or a {from, to} mapping."""
    if value is None:
        return None
    if isinstance(value, dict):
        pair = [value.get("from", value.get("start")), value.get("to", value.get("end"))]
        if None in pair:
            raise ValueError("a date range needs both `from` and `to`")
    elif isinstance(value, str | date):
        raise ValueError(f"a date range is [from, to], not a single value: {value!r}")
    else:
        pair = list(value)
    if len(pair) != 2:
        raise ValueError(f"a date range is [from, to]; got {value!r}")
    start, end = (_as_date(v) for v in pair)
    if start > end:
        raise ValueError(f"date range is [from, to]; got [{start}, {end}]")
    return (start, end)


@dataclass(frozen=True, slots=True)
class Selection:
    """Constraints on the population one slice of a run draws from.

    Every field is optional and an omitted one means *unconstrained* — the
    pack's normal behaviour. That default matters: it is what keeps a selection
    additive, so a run with no selection at all behaves exactly as it did before
    selections existed.
    """

    #: Restrict issuers to these locales, e.g. ``("fr-FR", "fr-CA")``.
    locales: tuple[str, ...] = ()
    #: Pin to these company ids exactly.
    companies: tuple[str, ...] = ()
    #: …or "use N companies", chosen reproducibly from the run id.
    company_count: int | None = None
    #: Restrict to these template names.
    archetypes: tuple[str, ...] = ()
    #: …or "use N templates".
    archetype_count: int | None = None
    #: Restrict issuers to these business types (pack vocabulary).
    business_types: tuple[str, ...] = ()
    #: Capture conditions to draw from. Empty means every document is CLEAN.
    conditions: tuple[DocumentCondition, ...] = ()
    #: Wear range drawn per document. None pins wear at its 1.0 default.
    wear: tuple[float, float] | None = None
    #: Delivery notes with a receiver's signature. Implies handwritten.
    goods_receipt: bool = False
    #: Draw each invoice's issue date uniformly from this ``(from, to)`` range
    #: (inclusive). None keeps the pack's default window. Every other date on the
    #: document — due date, payment received, billing period — is derived from the
    #: drawn issue date, so the whole document stays logically consistent with it.
    issue_date_range: tuple[date, date] | None = None
    #: Apply the per-business-type era floor when drawing issue dates — a *soft*
    #: realism default, not a hard rule. True (default) keeps e.g. an AI-platform
    #: issuer at or after its era even inside an older window; set False to let a
    #: run deliberately produce anachronistic dates. It never blocks a run either
    #: way — the pack has no idea what an "era" is; only the invoice sampler does.
    enforce_date_era: bool = True

    def __post_init__(self) -> None:
        if self.companies and self.company_count is not None:
            raise ValueError("give `companies` a list or a count, not both")
        if self.archetypes and self.archetype_count is not None:
            raise ValueError("give `archetypes` a list or a count, not both")
        if self.issue_date_range is not None:
            start, end = self.issue_date_range
            if start > end:
                raise ValueError(f"issue_date_range is (from, to); got ({start}, {end})")
        if self.goods_receipt and self.conditions not in ((), (DocumentCondition.HANDWRITTEN,)):
            raise ValueError(
                "a goods receipt is a handwritten delivery note, so it cannot also "
                f"be {'/'.join(c.value for c in self.conditions)}"
            )

    @property
    def is_empty(self) -> bool:
        """True when nothing is constrained — the pack draws as it always did."""
        return self == Selection()

    @property
    def effective_conditions(self) -> tuple[DocumentCondition, ...]:
        """What to actually draw from, with the goods-receipt implication applied."""
        if self.goods_receipt:
            return (DocumentCondition.HANDWRITTEN,)
        return self.conditions or (DocumentCondition.CLEAN,)

    @classmethod
    def from_mapping(cls, data: dict[str, Any] | None) -> Selection:
        """Parse one slice's composition block.

        Accepts exactly the vocabulary of ``run.example.yaml``, including its
        shorthands: a bare string where a list is allowed, ``all`` for
        unconstrained, an integer for "use N of them", and ``condition``
        singular alongside ``conditions`` plural.

        Sizing keys (``count``, ``share``) and identity keys (``name``, ``pack``,
        ``format``) are ignored here — they are the *run's* business, not the
        composition's, and a slice block legitimately carries both.
        """
        if not data:
            return cls()
        raw_conditions = data.get("conditions", data.get("condition"))
        conditions = tuple(
            DocumentCondition(c) for c in _names(raw_conditions, "condition")
        )
        return cls(
            locales=_names(data.get("locales"), "locales"),
            companies=_names(data.get("companies"), "companies")
            if _count(data.get("companies"), "companies") is None else (),
            company_count=_count(data.get("companies"), "companies"),
            archetypes=_names(data.get("archetypes"), "archetypes")
            if _count(data.get("archetypes"), "archetypes") is None else (),
            archetype_count=_count(data.get("archetypes"), "archetypes"),
            business_types=_names(data.get("business_types"), "business_types"),
            conditions=conditions,
            wear=_wear(data.get("wear")),
            goods_receipt=bool(data.get("goods_receipt", False)),
            issue_date_range=_date_range(data.get("date_range", data.get("issue_dates"))),
            enforce_date_era=bool(data.get("enforce_date_era", True)),
        )

    def merged(self, **overrides: Any) -> Selection:
        """A copy with some fields replaced — how CLI flags layer over a file."""
        return replace(self, **{k: v for k, v in overrides.items() if v is not None})

    def describe(self) -> str:
        """One line, for a plan or a log. Empty selections say so."""
        if self.is_empty:
            return "unconstrained"
        parts: list[str] = []
        if self.locales:
            parts.append(f"locales={','.join(self.locales)}")
        if self.companies:
            parts.append(f"companies=[{','.join(self.companies)}]")
        if self.company_count:
            parts.append(f"companies={self.company_count}x")
        if self.archetypes:
            parts.append(f"archetypes=[{','.join(self.archetypes)}]")
        if self.archetype_count:
            parts.append(f"archetypes={self.archetype_count}x")
        if self.business_types:
            parts.append(f"business_types={','.join(self.business_types)}")
        if self.conditions:
            parts.append(f"condition={','.join(c.value for c in self.conditions)}")
        if self.wear:
            parts.append(f"wear={self.wear[0]:g}-{self.wear[1]:g}")
        if self.goods_receipt:
            parts.append("goods_receipt")
        if self.issue_date_range:
            start, end = self.issue_date_range
            parts.append(f"dates={start}..{end}")
        if not self.enforce_date_era:
            parts.append("date-era=off")
        return " ".join(parts)


class UnsupportedConstraint(ValueError):
    """Raised by a pack asked for a composition it cannot produce.

    Loud on purpose, and raised at *resolve* time — before the first document —
    rather than per document. A silently ignored constraint is the failure this
    whole module exists to end: a slice named ``french`` that quietly emits
    English invoices costs a full run before anyone notices, and the documents
    look perfectly fine. There is nothing to catch it but a query nobody thought
    to run.
    """

    def __init__(self, message: str, *, field: str = "", known: tuple[str, ...] = ()) -> None:
        if known:
            message = f"{message} (known {field}: {', '.join(sorted(known))})"
        super().__init__(message)
