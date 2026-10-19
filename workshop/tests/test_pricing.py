from workshop.pricing import bulk_discount_rate, line_total, price_order


def test_bulk_tiers():
    assert bulk_discount_rate(1) == 0
    assert bulk_discount_rate(9) == 0
    assert bulk_discount_rate(10) == 5
    assert bulk_discount_rate(49) == 5
    assert bulk_discount_rate(50) == 10


def test_line_total_applies_bulk_discount():
    assert line_total(1000, 3) == 3000
    assert line_total(1000, 10) == 9500  # 5% off 10000


def test_tax_is_charged_after_discount():
    # 10 x 1000c with 10% tax: subtotal 9500, tax 950, total 10450.
    priced = price_order([(1000, 10)], 10)
    assert priced == {"subtotal": 9500, "tax": 950, "total": 10450}


def test_no_tax_no_discount():
    priced = price_order([(250, 2), (100, 1)], 0)
    assert priced == {"subtotal": 600, "tax": 0, "total": 600}
