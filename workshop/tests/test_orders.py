import pytest

from workshop.inventory import StockLedger
from workshop.orders import Order, OrderError


def stocked_ledger():
    ledger = StockLedger()
    ledger.add_stock("WDG-1", 20)
    ledger.add_stock("GAD-2", 5)
    return ledger


def test_place_reserves_and_prices():
    ledger = stocked_ledger()
    order = Order(ledger, [("WDG-1", 1000, 2)], tax_rate_percent=10)
    totals = order.place()
    assert order.status == "placed"
    assert totals == {"subtotal": 2000, "tax": 200, "total": 2200}
    assert ledger.available("WDG-1") == 18


def test_place_is_all_or_nothing():
    ledger = stocked_ledger()
    order = Order(ledger, [("WDG-1", 1000, 2), ("GAD-2", 500, 9)])
    with pytest.raises(OrderError):
        order.place()
    assert order.status == "open"
    assert ledger.available("WDG-1") == 20  # first line rolled back


def test_cancel_releases_reserved_stock():
    ledger = stocked_ledger()
    order = Order(ledger, [("WDG-1", 1000, 6)])
    order.place()
    assert ledger.available("WDG-1") == 14
    order.cancel()
    assert order.status == "cancelled"
    assert ledger.available("WDG-1") == 20


def test_cancel_twice_is_safe():
    ledger = stocked_ledger()
    order = Order(ledger, [("WDG-1", 1000, 6)])
    order.place()
    order.cancel()
    order.cancel()
    assert ledger.available("WDG-1") == 20
