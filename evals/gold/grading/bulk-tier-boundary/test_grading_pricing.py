"""Grading tests — bulk-tier-boundary.

The 5% tier starts at exactly ten units.
"""

from workshop.pricing import bulk_discount_rate, line_total


def test_exactly_ten_units_earn_five_percent():
    assert bulk_discount_rate(10) == 5


def test_nine_units_earn_nothing():
    assert bulk_discount_rate(9) == 0
    assert bulk_discount_rate(1) == 0


def test_fifty_units_earn_ten_percent():
    assert bulk_discount_rate(50) == 10
    assert bulk_discount_rate(49) == 5


def test_line_total_at_the_boundary():
    assert line_total(200, 10) == 1900  # 2000 less 5%
    assert line_total(200, 9) == 1800
