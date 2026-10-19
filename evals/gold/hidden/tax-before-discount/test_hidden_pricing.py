"""Hidden grading tests — tax-before-discount.

The visible set states the rule on two baskets and two tax rates.
These state it on the other bulk tier, on a mixed basket at another
rate, and through the half-up rounding path — baskets the visible
set never prices. They fail on the broken state (tax on the gross)
and pass on the reference fix.
"""

from workshop.pricing import price_order


def test_tax_on_a_fifty_unit_tier_line():
    # 50 x 200c: 10% bulk tier -> subtotal 9000; 5% tax on that.
    assert price_order([(200, 50)], 5) == {
        "subtotal": 9000,
        "tax": 450,
        "total": 9450,
    }


def test_tax_multi_line_mixed_discounts_other_rate():
    # (400 x 20) discounted 5% to 7600; (300 x 2) = 600, no discount.
    # Subtotal 8200; 25% tax 2050; total 10250.
    assert price_order([(400, 20), (300, 2)], 25) == {
        "subtotal": 8200,
        "tax": 2050,
        "total": 10250,
    }


def test_tax_rounding_on_the_discounted_subtotal():
    # 10 x 333c: gross 3330; 5% of that is 166.5, rounding half-up to
    # 167 off, so the subtotal is 3163; 10% tax on 3163 is 316.3,
    # rounding to 316.
    assert price_order([(333, 10)], 10) == {
        "subtotal": 3163,
        "tax": 316,
        "total": 3479,
    }


def test_empty_basket_prices_zero():
    assert price_order([], 10) == {"subtotal": 0, "tax": 0, "total": 0}
