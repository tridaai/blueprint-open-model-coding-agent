"""Pricing: line totals, bulk discounts, and tax.

All money is integer cents. Discounts come off the line subtotal
first; tax is charged on the discounted subtotal. Percentages are
whole numbers (5 means 5%), and cent rounding is half-up so a price
never depends on binary float behaviour.
"""

from __future__ import annotations


def _round_half_up(numerator: int, denominator: int) -> int:
    """Round numerator/denominator to the nearest integer, halves up."""
    return (2 * numerator + denominator) // (2 * denominator)


def _percent_of(cents: int, percent: int) -> int:
    """``percent`` percent of ``cents``, rounded half-up to a cent."""
    return _round_half_up(cents * percent, 100)


def bulk_discount_rate(qty: int) -> int:
    """Bulk discount percent for a single line of ``qty`` units.

    10 or more units earn 5%; 50 or more earn 10%. Below 10 units
    there is no discount.
    """
    if qty >= 50:
        return 10
    if qty >= 10:
        return 5
    return 0


def line_total(unit_cents: int, qty: int) -> int:
    """The discounted total for one line, in cents."""
    gross = unit_cents * qty
    return gross - _percent_of(gross, bulk_discount_rate(qty))


def price_order(
    lines: list[tuple[int, int]],
    tax_rate_percent: int,
    coupon_percent: int = 0,
) -> dict:
    """Price an order.

    ``lines`` is a list of ``(unit_cents, qty)`` pairs. Returns a dict
    with ``subtotal`` (after discounts, before tax), ``tax``, and
    ``total``, all in cents. Bulk discounts come off each line
    first; a coupon percent, when given, comes off the discounted
    subtotal; tax is charged on what remains.
    """
    subtotal = sum(line_total(unit, qty) for unit, qty in lines)
    if coupon_percent:
        subtotal -= _percent_of(subtotal, coupon_percent)
    tax = _percent_of(subtotal, tax_rate_percent)
    return {"subtotal": subtotal, "tax": tax, "total": subtotal + tax}
