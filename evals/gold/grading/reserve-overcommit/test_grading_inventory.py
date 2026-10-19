"""Grading tests — reserve-overcommit.

A reservation that cannot be met in full is refused and changes
nothing; availability never goes negative.
"""

from workshop.inventory import StockLedger


def test_reserve_beyond_available_is_refused():
    ledger = StockLedger()
    ledger.add_stock("WDG-1", 5)
    assert ledger.reserve("WDG-1", 6) is False


def test_refused_reservation_changes_nothing():
    ledger = StockLedger()
    ledger.add_stock("WDG-1", 5)
    ledger.reserve("WDG-1", 2)
    assert ledger.reserve("WDG-1", 4) is False
    assert ledger.reserved("WDG-1") == 2
    assert ledger.available("WDG-1") == 3


def test_availability_never_goes_negative():
    ledger = StockLedger()
    ledger.add_stock("WDG-1", 10)
    assert ledger.reserve("WDG-1", 6) is True
    assert ledger.reserve("WDG-1", 5) is False
    assert ledger.reserve("WDG-1", 4) is True
    assert ledger.available("WDG-1") == 0
    assert ledger.reserve("WDG-1", 1) is False
    assert ledger.available("WDG-1") == 0
