"""Stock ledger: what is on hand, what is reserved, what is available.

The ledger is the store's source of truth for stock. Orders reserve
stock before they are placed and release it when they are cancelled.
The invariant the rest of the library leans on: available stock for a
SKU never goes negative, and a reservation that cannot be met changes
nothing.
"""

from __future__ import annotations


class UnknownSkuError(KeyError):
    """Raised when an operation names a SKU the ledger has never seen."""


class StockLedger:
    """On-hand and reserved quantities per SKU."""

    def __init__(self) -> None:
        self._on_hand: dict[str, int] = {}
        self._reserved: dict[str, int] = {}

    # -- stock in -----------------------------------------------------
    def add_stock(self, sku: str, qty: int) -> None:
        """Add ``qty`` units of ``sku`` to the shelf."""
        if qty <= 0:
            raise ValueError("qty must be positive")
        self._on_hand[sku] = self._on_hand.get(sku, 0) + qty
        self._reserved.setdefault(sku, 0)

    # -- reads ----------------------------------------------------------
    def on_hand(self, sku: str) -> int:
        """Units physically on the shelf for ``sku``."""
        return self._on_hand.get(sku, 0)

    def reserved(self, sku: str) -> int:
        """Units of ``sku`` held by placed orders."""
        return self._reserved.get(sku, 0)

    def available(self, sku: str) -> int:
        """Units of ``sku`` a new order could still reserve."""
        return self.on_hand(sku) - self.reserved(sku)

    def skus(self) -> list[str]:
        """Every SKU the ledger knows, sorted."""
        return sorted(self._on_hand)

    # -- reservations ---------------------------------------------------
    def reserve(self, sku: str, qty: int) -> bool:
        """Reserve ``qty`` units of ``sku``.

        Returns True when the reservation was made. Returns False —
        and changes nothing — when fewer than ``qty`` units are
        available.
        """
        if qty <= 0:
            raise ValueError("qty must be positive")
        if sku not in self._on_hand:
            raise UnknownSkuError(sku)
        self._reserved[sku] = self._reserved.get(sku, 0) + qty
        return True

    def release(self, sku: str, qty: int) -> None:
        """Give ``qty`` reserved units of ``sku`` back to the shelf.

        Releasing more than is reserved clamps at zero; a release can
        never make the reserved count negative.
        """
        if qty <= 0:
            raise ValueError("qty must be positive")
        held = self._reserved.get(sku, 0)
        self._reserved[sku] = max(0, held - qty)

    # -- reporting --------------------------------------------------------
    def low_stock(self, threshold: int) -> list[str]:
        """SKUs whose *available* stock is at or below ``threshold``.

        Sorted by SKU so reports are stable. A SKU fully reserved
        counts as low even when the shelf itself is full.
        """
        return sorted(
            sku for sku in self._on_hand if self.available(sku) <= threshold
        )
