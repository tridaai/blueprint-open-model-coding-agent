"""Orders: reserve stock, price the basket, place or cancel.

An order is a list of lines against a :class:`StockLedger`. Placing
an order reserves every line's stock — all of it or none of it — and
freezes the priced totals. Cancelling a placed order releases its
reservations back to the shelf.
"""

from __future__ import annotations

from .inventory import StockLedger
from .pricing import price_order


class OrderError(Exception):
    """Raised when an order cannot be placed or changed."""


class Order:
    """One customer's basket.

    ``lines`` is a list of ``(sku, unit_cents, qty)`` triples.
    """

    def __init__(
        self,
        ledger: StockLedger,
        lines: list[tuple[str, int, int]],
        tax_rate_percent: int = 0,
        coupon_percent: int = 0,
    ) -> None:
        self._ledger = ledger
        self._lines = list(lines)
        self._tax_rate_percent = tax_rate_percent
        self._coupon_percent = coupon_percent
        self.status = "open"
        self.totals: dict | None = None

    @property
    def lines(self) -> list[tuple[str, int, int]]:
        return list(self._lines)

    def place(self) -> dict:
        """Reserve the stock and freeze the totals.

        Reservations are all-or-nothing: if any line cannot be
        reserved, every reservation this call already made is
        released and :class:`OrderError` is raised.
        """
        if self.status != "open":
            raise OrderError(f"cannot place an order in state {self.status}")
        made: list[tuple[str, int]] = []
        try:
            for sku, _unit, qty in self._lines:
                if not self._ledger.reserve(sku, qty):
                    raise OrderError(f"insufficient stock for {sku}")
                made.append((sku, qty))
        except Exception:
            for sku, qty in made:
                self._ledger.release(sku, qty)
            raise
        self.totals = price_order(
            [(unit, qty) for _sku, unit, qty in self._lines],
            self._tax_rate_percent,
            self._coupon_percent,
        )
        self.status = "placed"
        return self.totals

    def cancel(self) -> None:
        """Cancel the order, releasing any stock it reserved.

        Cancelling an open order simply closes it. Cancelling twice
        is safe: the second call changes nothing.
        """
        if self.status == "placed":
            for sku, _unit, qty in self._lines:
                self._ledger.release(sku, qty)
        if self.status in ("open", "placed"):
            self.status = "cancelled"
