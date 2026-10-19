"""Grading tests for the order-coupon gold task.

They fail on the broken state (coupons exist nowhere: price_order and
Order both reject the coupon argument) and on either half-fix, and
pass only when the coupon works end to end.
"""

from __future__ import annotations

from workshop.inventory import StockLedger
from workshop.orders import Order
from workshop.pricing import price_order


def test_coupon_comes_off_the_discounted_subtotal_before_tax():
    # 10 x 1000c: bulk brings the line to 9500; a 10% coupon takes 950
    # off that; 10% tax is charged on the remaining 8550.
    priced = price_order([(1000, 10)], 10, coupon_percent=10)
    assert priced == {"subtotal": 8550, "tax": 855, "total": 9405}


def test_no_coupon_means_exactly_todays_numbers():
    expected = {"subtotal": 9500, "tax": 950, "total": 10450}
    assert price_order([(1000, 10)], 10) == expected
    assert price_order([(1000, 10)], 10, coupon_percent=0) == expected


def test_coupon_rounds_half_up_like_everything_else():
    # 3 x 333c, no bulk tier: subtotal 999; 10% of that is 99.9, which
    # rounds half-up to 100 off.
    priced = price_order([(333, 3)], 0, coupon_percent=10)
    assert priced == {"subtotal": 899, "tax": 0, "total": 899}


def test_an_order_carries_its_coupon_into_the_frozen_totals():
    ledger = StockLedger()
    ledger.add_stock("WIDGET", 20)
    order = Order(
        ledger, [("WIDGET", 1000, 10)], tax_rate_percent=10, coupon_percent=10
    )
    totals = order.place()
    assert totals == {"subtotal": 8550, "tax": 855, "total": 9405}
    assert order.status == "placed"
