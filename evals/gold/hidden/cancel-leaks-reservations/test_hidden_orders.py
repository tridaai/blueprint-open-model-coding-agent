"""Hidden grading tests — cancel-leaks-reservations.

The visible set states that cancelling releases the lines. These
state what that release is *for* and where its edges are: freed
stock can be sold again, duplicate-SKU lines release in full, a
failed placement releases nothing when cancelled, and the frozen
totals survive the cancel. They fail on the broken state wherever
a release is owed, and pass on the reference fix.
"""

import pytest

from workshop.inventory import StockLedger
from workshop.orders import Order, OrderError


def test_freed_stock_can_be_reserved_by_a_new_order():
    ledger = StockLedger()
    ledger.add_stock("WDG-1", 20)
    ledger.add_stock("GAD-2", 8)
    first = Order(ledger, [("WDG-1", 1000, 6), ("GAD-2", 500, 3)])
    first.place()
    first.cancel()
    second = Order(ledger, [("WDG-1", 1000, 6), ("GAD-2", 500, 3)])
    totals = second.place()
    assert second.status == "placed"
    assert totals is not None
    assert ledger.available("WDG-1") == 14
    assert ledger.available("GAD-2") == 5


def test_cancel_releases_duplicate_sku_lines_in_full():
    ledger = StockLedger()
    ledger.add_stock("WDG-1", 20)
    order = Order(ledger, [("WDG-1", 1000, 2), ("WDG-1", 1000, 3)])
    order.place()
    assert ledger.available("WDG-1") == 15
    order.cancel()
    assert ledger.reserved("WDG-1") == 0
    assert ledger.available("WDG-1") == 20


def test_cancel_after_a_failed_place_releases_nothing():
    ledger = StockLedger()
    ledger.add_stock("GAD-2", 5)
    order = Order(ledger, [("GAD-2", 500, 9)])
    with pytest.raises(OrderError):
        order.place()
    order.cancel()
    assert order.status == "cancelled"
    assert ledger.available("GAD-2") == 5


def test_cancel_keeps_the_frozen_totals():
    ledger = StockLedger()
    ledger.add_stock("WDG-1", 20)
    order = Order(ledger, [("WDG-1", 1000, 6)], tax_rate_percent=10)
    totals = dict(order.place())
    order.cancel()
    assert order.status == "cancelled"
    assert order.totals == totals
