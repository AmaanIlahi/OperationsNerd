"""Deterministic supplier rules for the standalone, in-memory prototype.

This is a Python port of the current demo's quantity/deadline/budget checks and
lowest-cost, earlier-delivery, supplier-ID ordering in ``src/domain/compare.ts``.
It deliberately does not import the browser code, estimate cache, or an LLM.
The extension here is exact split sourcing. A fixed-total quote prices *exactly*
``quantity_priced`` units; a unit quote prices 1 through
``min(capacity_quantity, quantity_priced)`` units. Capacity is never a price.

Budget means the combined cost of purchased shortages, excluding stock already
owned. Plans first minimize total cost. Equal-cost split plans minimize units
arriving on the latest date, then the next latest date, and so on; remaining
ties favor lexically earlier supplier IDs. For a single-source plan this is the
existing price, expected-date, supplier-ID ordering. Input order has no effect.
Delivery dates use UTC calendar dates for v1: a delivery date before the current
UTC date is stale even if the quote has not expired; the same UTC date is valid.

The exact bounded-knapsack search supports at most 2,000 required units, 32
quotes, and 500,000 examined transitions. Exceeding a bound raises ValueError;
the prototype never silently substitutes a heuristic. A production adapter
must decide on suitable limits or a different solver before enabling it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
import re


MAX_NEEDED = 2_000
MAX_QUOTES = 32
MAX_TRANSITIONS = 500_000


def _integer(name: str, value: int, *, minimum: int = 0) -> None:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}; got {value!r}.")


def _identifier(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty identifier.")


def _currency(value: str) -> None:
    if not isinstance(value, str) or re.fullmatch(r"[A-Z]{3}", value) is None:
        raise ValueError("Currency must be an explicitly configured uppercase three-letter code.")


def _aware(name: str, value: datetime) -> None:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime.")


def _date(name: str, value: date) -> None:
    if type(value) is not date:
        raise ValueError(f"{name} must be a calendar date, not a datetime or string.")


@dataclass(frozen=True)
class Quote:
    """Immutable quote facts; synthetic and AI values are estimates only."""

    id: str
    business_id: str
    line_id: str
    supplier_id: str
    capacity_quantity: int
    quantity_priced: int
    price_cents: int
    pricing_kind: str
    currency: str
    source: str
    generated_at: datetime
    expires_at: datetime
    expected_date: date

    def __post_init__(self) -> None:
        for name in ("id", "business_id", "line_id", "supplier_id"):
            _identifier(name, getattr(self, name))
        _integer("capacity_quantity", self.capacity_quantity)
        _integer("quantity_priced", self.quantity_priced, minimum=1)
        _integer("price_cents", self.price_cents)
        if self.pricing_kind not in ("unit", "total"):
            raise ValueError("pricing_kind must be 'unit' or 'total'.")
        _currency(self.currency)
        if self.source not in ("synthetic", "ai_estimate", "supplier"):
            raise ValueError("source must be 'synthetic', 'ai_estimate', or 'supplier'.")
        _aware("generated_at", self.generated_at)
        _aware("expires_at", self.expires_at)
        if self.expires_at <= self.generated_at:
            raise ValueError("expires_at must be after generated_at.")
        _date("expected_date", self.expected_date)

    @property
    def estimated(self) -> bool:
        return self.source != "supplier"

    def cost(self, quantity: int) -> int:
        """Return integer cents only for quantities actually covered by terms."""
        _integer("quantity", quantity, minimum=1)
        if quantity > self.capacity_quantity:
            raise ValueError(
                f"Quote {self.id} capacity {self.capacity_quantity} is below purchased quantity {quantity}."
            )
        if self.pricing_kind == "total":
            if quantity != self.quantity_priced:
                raise ValueError(
                    f"Quote {self.id} total price covers exactly {self.quantity_priced} units, not {quantity}."
                )
            return self.price_cents
        if quantity > self.quantity_priced:
            raise ValueError(
                f"Quote {self.id} unit terms price at most {self.quantity_priced} units, not {quantity}."
            )
        return quantity * self.price_cents

    def validate_at(self, now: datetime, deadline: date, currency: str) -> None:
        """Revalidate validity and delivery using today's UTC calendar date."""
        _aware("now", now)
        _date("deadline", deadline)
        _currency(currency)
        failures = []
        if self.currency != currency:
            failures.append(
                f"Quote {self.id} currency {self.currency} differs from configured {currency}; mixed currencies are unsupported."
            )
        if now < self.generated_at:
            failures.append(
                f"Quote {self.id} was generated at {self.generated_at.isoformat()}, after approval/comparison time {now.isoformat()}."
            )
        if now >= self.expires_at:
            failures.append(
                f"Quote {self.id} expired at {self.expires_at.isoformat()}; current time is {now.isoformat()}."
            )
        utc_today = now.astimezone(timezone.utc).date()
        if self.expected_date < utc_today:
            failures.append(
                f"Quote {self.id} delivery {self.expected_date.isoformat()} is before current UTC date {utc_today.isoformat()}."
            )
        if self.expected_date > deadline:
            failures.append(
                f"Quote {self.id} delivery {self.expected_date.isoformat()} is after deadline {deadline.isoformat()}."
            )
        if failures:
            raise ValueError(" ".join(failures))


@dataclass(frozen=True)
class Allocation:
    quote_id: str
    quantity: int
    total_cents: int

    def __post_init__(self) -> None:
        _identifier("quote_id", self.quote_id)
        _integer("quantity", self.quantity, minimum=1)
        _integer("total_cents", self.total_cents)


@dataclass(frozen=True)
class Recommendation:
    """A recommendation has no approval or purchasing side effects.

    Infeasible results contain no allocations and total_cents == 0; explanations
    include the cheapest plan's actual cost when it exceeds the budget.
    """

    feasible: bool
    allocations: tuple[Allocation, ...]
    total_cents: int
    explanations: tuple[str, ...]


@dataclass(frozen=True)
class _Plan:
    score: tuple[int, int, int]
    quantities: tuple[int, ...]


def select_quotes(
    needed: int,
    quotes: tuple[Quote, ...],
    *,
    now: datetime,
    deadline: date,
    currency: str,
    budget_cents: int | None,
) -> Recommendation:
    """Find an exact, minimum-cost allocation of remaining unallocated demand.

    Callers must pass only unused quotes belonging to their authenticated company
    and demand. This pure function also rejects mixed companies/lines, duplicate
    IDs/suppliers, and mixed currencies. It cannot authenticate a caller.
    """
    _integer("needed", needed)
    _aware("now", now)
    _date("deadline", deadline)
    _currency(currency)
    if budget_cents is not None:
        _integer("budget_cents", budget_cents)
    if needed > MAX_NEEDED:
        raise ValueError(f"Standalone exact selection supports at most {MAX_NEEDED} needed units; got {needed}.")
    if not isinstance(quotes, tuple) or any(not isinstance(quote, Quote) for quote in quotes):
        raise ValueError("quotes must be a tuple of immutable Quote values.")
    if len(quotes) > MAX_QUOTES:
        raise ValueError(f"Standalone exact selection supports at most {MAX_QUOTES} quotes; got {len(quotes)}.")
    if len({quote.id for quote in quotes}) != len(quotes):
        raise ValueError("Duplicate quote IDs are not permitted.")
    if len({quote.supplier_id for quote in quotes}) != len(quotes):
        raise ValueError("Duplicate supplier quotes are not permitted; capacity cannot be stacked.")
    if len({quote.business_id for quote in quotes}) > 1:
        raise ValueError("Quotes from different companies cannot serve the same demand.")
    if len({quote.line_id for quote in quotes}) > 1:
        raise ValueError("Quotes from different order lines cannot serve the same demand.")
    if any(quote.currency != currency for quote in quotes):
        raise ValueError(f"Mixed currencies are unsupported; all quotes must use configured {currency}.")
    if needed == 0:
        return Recommendation(True, (), 0, ("No unallocated shortage remains; no purchase is recommended.",))

    explanations: list[str] = []
    candidates: list[Quote] = []
    for quote in sorted(quotes, key=lambda value: (value.expected_date, value.supplier_id, value.id)):
        try:
            quote.validate_at(now, deadline, currency)
        except ValueError as error:
            explanations.append(str(error))
            continue
        if quote.capacity_quantity == 0:
            explanations.append(
                f"Quote {quote.id} capacity 0 cannot cover any of remaining shortage {needed}."
            )
            continue
        if quote.pricing_kind == "total" and quote.quantity_priced > quote.capacity_quantity:
            explanations.append(
                f"Quote {quote.id} capacity {quote.capacity_quantity} is below its fixed priced quantity {quote.quantity_priced}."
            )
            continue
        if quote.pricing_kind == "total" and quote.quantity_priced > needed:
            explanations.append(
                f"Quote {quote.id} prices exactly {quote.quantity_priced} units, above remaining shortage {needed}; oversupply is not permitted."
            )
            continue
        candidates.append(quote)

    # Positional integer weights implement additive lexicographic count vectors.
    # Base > total units prevents carries; adding the same future allocation to
    # two plans preserves score order, which makes one best plan/subtotal valid.
    dates = sorted({quote.expected_date for quote in candidates}, reverse=True)
    suppliers = sorted(quote.supplier_id for quote in candidates)
    base = needed + 1
    date_weight = {value: base ** (len(dates) - index - 1) for index, value in enumerate(dates)}
    supplier_weight = {value: base ** (len(suppliers) - index - 1) for index, value in enumerate(suppliers)}
    plans = {0: _Plan((0, 0, 0), ())}
    transitions = 0
    for quote in candidates:
        next_plans: dict[int, _Plan] = {}
        for allocated, plan in plans.items():
            upper = min(needed - allocated, quote.capacity_quantity, quote.quantity_priced)
            options = (0, quote.quantity_priced) if quote.pricing_kind == "total" and quote.quantity_priced <= upper else (0,)
            if quote.pricing_kind == "unit":
                options = range(upper + 1)
            for quantity in options:
                transitions += 1
                if transitions > MAX_TRANSITIONS:
                    raise ValueError(
                        f"Exact split search exceeded {MAX_TRANSITIONS} transitions; reduce quantities/candidates or choose an approved production solver."
                    )
                price = quote.cost(quantity) if quantity else 0
                score = (
                    plan.score[0] + price,
                    plan.score[1] + quantity * date_weight[quote.expected_date],
                    plan.score[2] - quantity * supplier_weight[quote.supplier_id],
                )
                subtotal = allocated + quantity
                previous = next_plans.get(subtotal)
                if previous is None or score < previous.score:
                    next_plans[subtotal] = _Plan(score, plan.quantities + (quantity,))
        plans = next_plans

    best = plans.get(needed)
    if best is None:
        covered = max(plans)
        explanations.append(
            f"No exact allocation can cover remaining shortage {needed}; usable quoted terms can cover at most {covered} units without oversupply."
        )
        return Recommendation(False, (), 0, tuple(explanations))
    total_cents = best.score[0]
    if budget_cents is not None and total_cents > budget_cents:
        explanations.append(
            f"Cheapest combined purchase cost {total_cents} cents {currency} exceeds purchased-shortage budget {budget_cents} cents by {total_cents - budget_cents} cents."
        )
        return Recommendation(False, (), 0, tuple(explanations))

    allocations = []
    for quote, quantity in zip(candidates, best.quantities):
        if not quantity:
            continue
        price = quote.cost(quantity)
        allocations.append(Allocation(quote.id, quantity, price))
        provenance = f"{quote.source} estimate, not a supplier commitment" if quote.estimated else "supplier quote"
        explanations.append(
            f"Recommend {quantity} units from {quote.supplier_id} using quote {quote.id}, cost {price} cents {currency}; "
            f"delivery {quote.expected_date.isoformat()} <= deadline {deadline.isoformat()}; {provenance}."
        )
    budget_fact = f" <= budget {budget_cents} cents" if budget_cents is not None else "; no budget limit configured"
    explanations.append(
        f"Combined cost {total_cents} cents {currency}{budget_fact} covers {needed} units of purchased shortage. Explicit owner approval is required."
    )
    return Recommendation(True, tuple(allocations), total_cents, tuple(explanations))
