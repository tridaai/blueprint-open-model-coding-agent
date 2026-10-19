"""Workshop — a small stock and ordering library.

Workshop is the sample codebase the coding agent works on. It tracks
stock for a small parts store, prices orders, and places and cancels
orders against the stock ledger. Money is integer cents throughout;
quantities are whole units.
"""

from .inventory import StockLedger, UnknownSkuError
from .orders import Order, OrderError
from .pricing import bulk_discount_rate, price_order

__all__ = [
    "Order",
    "OrderError",
    "StockLedger",
    "UnknownSkuError",
    "bulk_discount_rate",
    "price_order",
]
