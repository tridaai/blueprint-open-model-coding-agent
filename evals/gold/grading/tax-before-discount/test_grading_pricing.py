"""Grading tests — tax-before-discount.

Tax is charged on the discounted subtotal, never the gross.
"""

from workshop.pricing import price_order


def test_tax_uses_discounted_subtotal_single_line():
    # 10 x 1000c: 5% bulk discount -> subtotal 9500; 10% tax on that.
    assert price_order([(1000, 10)], 10) == {
        "subtotal": 9500,
        "tax": 950,
        "total": 10450,
    }


def test_tax_uses_discounted_subtotal_multi_line():
    # (1000 x 10) discounted to 9500; (500 x 4) = 2000, no discount.
    # Subtotal 11500; 10% tax 1150; total 12650.
    assert price_order([(1000, 10), (500, 4)], 10) == {
        "subtotal": 11500,
        "tax": 1150,
        "total": 12650,
    }


def test_undiscounted_basket_is_unchanged():
    assert price_order([(100, 3)], 20) == {
        "subtotal": 300,
        "tax": 60,
        "total": 360,
    }
