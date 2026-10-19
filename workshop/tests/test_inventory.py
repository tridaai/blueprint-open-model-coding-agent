import pytest

from workshop.inventory import StockLedger, UnknownSkuError


def test_add_and_read_back():
    ledger = StockLedger()
    ledger.add_stock("WDG-1", 10)
    assert ledger.on_hand("WDG-1") == 10
    assert ledger.available("WDG-1") == 10
    assert ledger.skus() == ["WDG-1"]


def test_reserve_reduces_availability():
    ledger = StockLedger()
    ledger.add_stock("WDG-1", 10)
    assert ledger.reserve("WDG-1", 4) is True
    assert ledger.available("WDG-1") == 6
    assert ledger.reserved("WDG-1") == 4


def test_reserve_beyond_available_changes_nothing():
    ledger = StockLedger()
    ledger.add_stock("WDG-1", 3)
    assert ledger.reserve("WDG-1", 4) is False
    assert ledger.available("WDG-1") == 3
    assert ledger.reserved("WDG-1") == 0


def test_reserve_unknown_sku_raises():
    ledger = StockLedger()
    with pytest.raises(UnknownSkuError):
        ledger.reserve("NOPE", 1)


def test_release_clamps_at_zero():
    ledger = StockLedger()
    ledger.add_stock("WDG-1", 5)
    ledger.reserve("WDG-1", 2)
    ledger.release("WDG-1", 9)
    assert ledger.reserved("WDG-1") == 0
    assert ledger.available("WDG-1") == 5


def test_low_stock_uses_available_and_sorts():
    ledger = StockLedger()
    ledger.add_stock("B-2", 5)
    ledger.add_stock("A-1", 20)
    ledger.add_stock("C-3", 8)
    ledger.reserve("A-1", 18)  # available 2 despite a full shelf
    assert ledger.low_stock(5) == ["A-1", "B-2"]
