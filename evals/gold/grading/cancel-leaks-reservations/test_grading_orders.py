"""Grading tests — cancel-leaks-reservations.

Cancelling a placed order releases every line's reservation.
"""

from workshop.inventory import StockLedger
from workshop.orders import Order


def ledger():
    led = StockLedger()
    led.add_stock("WDG-1", 20)
    led.add_stock("GAD-2", 8)
    return led


def test_cancel_restores_availability_for_every_line():
    led = ledger()
    order = Order(led, [("WDG-1", 1000, 6), ("GAD-2", 500, 3)])
    order.place()
    assert led.available("WDG-1") == 14
    assert led.available("GAD-2") == 5
    order.cancel()
    assert led.available("WDG-1") == 20
    assert led.available("GAD-2") == 8


def test_double_cancel_releases_only_once():
    led = ledger()
    order = Order(led, [("WDG-1", 1000, 6)])
    order.place()
    order.cancel()
    order.cancel()
    assert led.reserved("WDG-1") == 0
    assert led.available("WDG-1") == 20


def test_cancel_open_order_closes_without_touching_stock():
    led = ledger()
    order = Order(led, [("WDG-1", 1000, 6)])
    order.cancel()
    assert order.status == "cancelled"
    assert led.available("WDG-1") == 20
