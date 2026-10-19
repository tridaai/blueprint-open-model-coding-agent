"""Hidden grading tests — order-coupon.

The visible set states the coupon on one basket, one rounding
case, and one order. These state it on the other bulk tier, across
several lines, at a full 100%, and on orders with other rates —
shapes the visible set never prices. They fail on the broken state
(no coupon exists anywhere) and pass on the reference fix.
"""

from workshop.inventory import StockLedger
from workshop.orders import Order
from workshop.pricing import price_order


def test_coupon_on_a_fifty_unit_tier_line():
    # 50 x 200c: bulk brings the line to 9000; a 20% coupon takes
    # 1800 off that; 10% tax is charged on the remaining 7200.
    priced = price_order([(200, 50)], 10, coupon_percent=20)
    assert priced == {"subtotal": 7200, "tax": 720, "total": 7920}


def test_a_full_coupon_zeroes_subtotal_and_tax():
    priced = price_order([(100, 3)], 10, coupon_percent=100)
    assert priced == {"subtotal": 0, "tax": 0, "total": 0}


def test_coupon_applies_across_lines_after_line_discounts():
    # (1000 x 10) discounted to 9500; (500 x 4) = 2000. Subtotal
    # 11500; a 10% coupon takes 1150 off the whole basket.
    priced = price_order([(1000, 10), (500, 4)], 0, coupon_percent=10)
    assert priced == {"subtotal": 10350, "tax": 0, "total": 10350}


def test_an_order_with_a_coupon_and_no_tax():
    ledger = StockLedger()
    ledger.add_stock("WIDGET", 20)
    order = Order(
        ledger, [("WIDGET", 500, 20)], tax_rate_percent=0, coupon_percent=5
    )
    # Line: 10000 less 5% bulk = 9500; coupon 5% of that is 475 off.
    assert order.place() == {"subtotal": 9025, "tax": 0, "total": 9025}
    assert order.status == "placed"


def test_an_order_with_an_explicit_zero_coupon_matches_no_coupon():
    ledger = StockLedger()
    ledger.add_stock("WIDGET", 20)
    order = Order(
        ledger, [("WIDGET", 1000, 10)], tax_rate_percent=10, coupon_percent=0
    )
    assert order.place() == {"subtotal": 9500, "tax": 950, "total": 10450}
