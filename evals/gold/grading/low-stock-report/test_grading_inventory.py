"""Grading tests — low-stock-report.

low_stock reports SKUs whose *available* stock is at or below the
threshold, sorted by SKU.
"""

from workshop.inventory import StockLedger


def test_reports_at_and_below_threshold_sorted():
    ledger = StockLedger()
    ledger.add_stock("B-2", 3)
    ledger.add_stock("A-1", 4)
    ledger.add_stock("C-3", 9)
    assert ledger.low_stock(4) == ["A-1", "B-2"]


def test_uses_available_not_on_hand():
    ledger = StockLedger()
    ledger.add_stock("A-1", 20)
    ledger.reserve("A-1", 17)  # only 3 available
    assert ledger.low_stock(3) == ["A-1"]
    assert ledger.low_stock(2) == []


def test_empty_when_everything_is_above():
    ledger = StockLedger()
    ledger.add_stock("A-1", 10)
    assert ledger.low_stock(2) == []
