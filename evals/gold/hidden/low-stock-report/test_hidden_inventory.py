"""Hidden grading tests — low-stock-report.

The visible set states the rule on one report and one reservation.
These state its edges: a zero threshold, a report that changes as
the threshold and the shelves move, and an empty ledger. They fail
on the broken state (the stub returns []) wherever the true report
is non-empty, and pass on the reference fix.
"""

from workshop.inventory import StockLedger


def test_threshold_zero_reports_only_fully_unavailable_skus():
    ledger = StockLedger()
    ledger.add_stock("A-1", 5)
    ledger.reserve("A-1", 5)  # shelf full, nothing available
    ledger.add_stock("B-2", 1)
    assert ledger.low_stock(0) == ["A-1"]


def test_the_report_moves_with_the_threshold():
    ledger = StockLedger()
    ledger.add_stock("Z-9", 2)
    ledger.add_stock("M-4", 9)
    ledger.add_stock("A-0", 9)
    assert ledger.low_stock(2) == ["Z-9"]
    assert ledger.low_stock(9) == ["A-0", "M-4", "Z-9"]


def test_restocking_lifts_a_sku_out_of_the_report():
    ledger = StockLedger()
    ledger.add_stock("A-1", 3)
    assert ledger.low_stock(5) == ["A-1"]
    ledger.add_stock("A-1", 10)
    assert ledger.low_stock(5) == []


def test_empty_ledger_reports_nothing():
    assert StockLedger().low_stock(10) == []
