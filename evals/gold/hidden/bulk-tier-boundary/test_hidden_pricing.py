"""Hidden grading tests — bulk-tier-boundary.

The visible set states the tiers at 1, 9, 10, 49, and 50 units, and
one line total at the boundary. These state the tiers inside their
ranges and the boundary at other prices and through a full priced
order. They fail on the broken state wherever ten units are priced
(the strict greater-than denies them the tier) and pass on the
reference fix.
"""

from workshop.pricing import bulk_discount_rate, line_total, price_order


def test_rates_inside_the_tiers():
    assert bulk_discount_rate(11) == 5
    assert bulk_discount_rate(25) == 5
    assert bulk_discount_rate(99) == 10
    assert bulk_discount_rate(500) == 10
    assert bulk_discount_rate(0) == 0


def test_line_total_at_ten_with_other_prices():
    assert line_total(250, 10) == 2375  # 2500 less 5%
    # 3330 less 5% (166.5, rounding half-up to 167).
    assert line_total(333, 10) == 3163


def test_line_total_at_fifty_and_just_below():
    assert line_total(100, 50) == 4500  # 5000 less 10%
    assert line_total(100, 49) == 4655  # 4900 less 5%


def test_price_order_at_ten_units_with_another_tax_rate():
    assert price_order([(250, 10)], 20) == {
        "subtotal": 2375,
        "tax": 475,
        "total": 2850,
    }
