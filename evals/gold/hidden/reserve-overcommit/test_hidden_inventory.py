"""Hidden grading tests — reserve-overcommit.

These assert behaviour the visible grading tests imply but do not
state, on SKUs and quantities the visible set never uses: an
overfit fix that special-cases the visible tests' inputs fails
here. They fail on the broken state and pass on the reference fix.
"""

from workshop.inventory import StockLedger


def test_reserving_exactly_what_is_available_succeeds_then_no_more():
    ledger = StockLedger()
    ledger.add_stock("BLT-7", 7)
    assert ledger.reserve("BLT-7", 7) is True
    assert ledger.available("BLT-7") == 0
    assert ledger.reserve("BLT-7", 1) is False
    assert ledger.reserved("BLT-7") == 7


def test_overcommit_on_one_sku_does_not_touch_another():
    ledger = StockLedger()
    ledger.add_stock("BLT-7", 4)
    ledger.add_stock("GAD-9", 6)
    assert ledger.reserve("BLT-7", 5) is False
    assert ledger.available("BLT-7") == 4
    assert ledger.reserve("GAD-9", 6) is True
    assert ledger.available("GAD-9") == 0


def test_refused_reservation_leaves_the_ledger_unchanged():
    ledger = StockLedger()
    ledger.add_stock("K-1", 3)
    assert ledger.reserve("K-1", 10) is False
    assert ledger.on_hand("K-1") == 3
    assert ledger.reserved("K-1") == 0
    assert ledger.skus() == ["K-1"]
