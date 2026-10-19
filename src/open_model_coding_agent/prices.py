"""The governed price table, and the folds that price usage with it.

Prices are data, not code, and they are governed the way profiles
are: versioned JSON records on disk (``prices/table-v<N>.json``),
loaded fail-closed. A record the loader does not fully understand
— an unknown field, a malformed price, a half-priced slug —
refuses by name at load; there is no best-effort parsing of money.
Changing a price means publishing a NEW table version alongside the
old one, never editing a version in place: every usage row is
stamped with the table version in force when it was recorded, and
every cost fold prices each row under its own version, so a
historical run keeps the prices it was costed under even after the
table moves on.

A slug with no recorded price — absent from the table, or recorded
with null prices because nobody has verified them yet — folds as
UNPRICED: its tokens are counted separately and never enter a money
total, and never silently count as zero cost either. The shipped
table (v1) records the shortlist's slugs with null prices on
purpose: the numbers a deployment prices with must be numbers it
verified against the provider's current pricing, and this repo does
not invent them.

All money arithmetic is :class:`decimal.Decimal`. Prices are
recorded as decimal strings in the JSON (``"0.30"`` per million
tokens); a JSON float is refused at load rather than converted,
because a float has already lost the exactness the ledger exists
to keep.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .errors import ConfigError
from .profiles import SLUG_SHAPE

_TABLE_FILE = re.compile(r"^table-v(\d+)\.json$")
_TABLE_FIELDS = {"version", "currency", "as_of", "source", "prices"}
_PRICE_FIELDS = {"input_per_mtok", "output_per_mtok"}
_PER_MTOK = Decimal(1_000_000)

# Why a usage row did not enter a money fold, in the fold's words.
REASON_NO_PRICE = "no_price_recorded"
REASON_SCRIPTED = "scripted_accounting"
REASON_NO_VERSION = "price_table_version_unrecorded"
REASON_VERSION_MISSING = "price_table_version_missing"


@dataclass(frozen=True)
class PriceTable:
    """One version of the price table, as recorded."""

    version: int
    currency: str
    as_of: str
    source: str
    # slug -> (input, output) price per million tokens; a slug maps
    # to None when the table records it with null prices — present
    # in the table, deliberately unpriced until verified.
    prices: dict[str, tuple[Decimal, Decimal] | None] = field(default_factory=dict)

    def price_for(self, slug: str | None) -> tuple[Decimal, Decimal] | None:
        """The recorded price for a slug, or None — the slug is
        unknown to the table, or recorded unpriced. Looking a slug
        up never raises: an unpriced model is a fact about the
        table, not an error in the fold."""
        if slug is None:
            return None
        return self.prices.get(slug)


def _refuse(problem: str) -> ConfigError:
    return ConfigError(f"price table: {problem}")


def _parse_price(value: object, *, slug: str, field_name: str, source: Path) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool) or isinstance(value, float):
        raise _refuse(
            f"{source.name}: price {field_name} for {slug!r} is {value!r}; "
            f"prices are recorded as decimal strings (e.g. \"0.30\") so the "
            f"ledger never inherits a float's rounding"
        )
    if isinstance(value, int):
        parsed = Decimal(value)
    elif isinstance(value, str):
        try:
            parsed = Decimal(value)
        except InvalidOperation:
            raise _refuse(
                f"{source.name}: price {field_name} for {slug!r} is not a "
                f"decimal: {value!r}"
            ) from None
    else:
        raise _refuse(
            f"{source.name}: price {field_name} for {slug!r} must be a "
            f"decimal string or null, got {type(value).__name__}"
        )
    if not parsed.is_finite() or parsed < 0:
        raise _refuse(
            f"{source.name}: price {field_name} for {slug!r} must be a "
            f"finite, non-negative decimal, got {value!r}"
        )
    return parsed


def _parse_table(raw: object, source: Path, file_version: int) -> PriceTable:
    if not isinstance(raw, dict):
        raise _refuse(f"{source.name} is not a JSON object")
    unknown = sorted(set(raw) - _TABLE_FIELDS)
    if unknown:
        raise _refuse(
            f"{source.name}: unknown field(s) {unknown}; the loader refuses "
            f"records it does not fully understand"
        )
    missing = sorted(_TABLE_FIELDS - set(raw))
    if missing:
        raise _refuse(f"{source.name}: missing field(s) {missing}")
    version = raw["version"]
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise _refuse(f"{source.name}: version must be a positive integer, got {version!r}")
    if version != file_version:
        raise _refuse(
            f"{source.name}: the file name says v{file_version} but the "
            f"record says v{version}; the two must agree"
        )
    currency = raw["currency"]
    if currency != "USD":
        raise _refuse(
            f"{source.name}: currency must be 'USD' (the ledger folds one "
            f"currency), got {currency!r}"
        )
    as_of = raw["as_of"]
    if not isinstance(as_of, str):
        raise _refuse(f"{source.name}: as_of must be an ISO date string")
    try:
        date.fromisoformat(as_of)
    except ValueError:
        raise _refuse(
            f"{source.name}: as_of must be an ISO date (YYYY-MM-DD), got {as_of!r}"
        ) from None
    source_note = raw["source"]
    if not isinstance(source_note, str) or not source_note:
        raise _refuse(f"{source.name}: source must be a non-empty note naming where the prices came from")
    raw_prices = raw["prices"]
    if not isinstance(raw_prices, dict):
        raise _refuse(f"{source.name}: prices must be an object keyed by model slug")
    prices: dict[str, tuple[Decimal, Decimal] | None] = {}
    for slug, entry in raw_prices.items():
        if not SLUG_SHAPE.match(slug):
            raise _refuse(f"{source.name}: {slug!r} is not a model slug in org/name form")
        if not isinstance(entry, dict):
            raise _refuse(f"{source.name}: the price entry for {slug!r} is not an object")
        extra = sorted(set(entry) - _PRICE_FIELDS)
        if extra:
            raise _refuse(f"{source.name}: unknown field(s) {extra} in the price entry for {slug!r}")
        if set(entry) != _PRICE_FIELDS:
            raise _refuse(
                f"{source.name}: the price entry for {slug!r} must carry both "
                f"input_per_mtok and output_per_mtok (null when unverified)"
            )
        input_price = _parse_price(entry["input_per_mtok"], slug=slug, field_name="input_per_mtok", source=source)
        output_price = _parse_price(entry["output_per_mtok"], slug=slug, field_name="output_per_mtok", source=source)
        if (input_price is None) != (output_price is None):
            raise _refuse(
                f"{source.name}: {slug!r} is half-priced (one of input/output "
                f"is null); price both sides, or neither — a half price "
                f"would silently misprice every call"
            )
        prices[slug] = (
            None if input_price is None else (input_price, output_price)
        )
    return PriceTable(
        version=version, currency=currency, as_of=as_of, source=source_note,
        prices=prices,
    )


def load_price_tables(prices_dir: Path) -> dict[int, PriceTable]:
    """Every recorded table version in the directory, loaded
    fail-closed: a directory with no table, or any malformed record
    in it, refuses by name at load. History is kept on disk — old
    versions stay loadable because old runs were costed under them.
    """
    folder = Path(prices_dir)
    found: dict[int, Path] = {}
    if folder.is_dir():
        for path in sorted(folder.glob("table-v*.json")):
            match = _TABLE_FILE.match(path.name)
            if match:
                found[int(match.group(1))] = path
    if not found:
        raise _refuse(
            f"no price table (table-v<N>.json) found in {folder}; the cost "
            f"ledger has nothing governed to price with — refusing to start"
        )
    tables: dict[int, PriceTable] = {}
    for version, path in sorted(found.items()):
        try:
            raw = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            raise _refuse(f"{path.name} is not valid JSON: {exc}") from None
        tables[version] = _parse_table(raw, path, version)
    return tables


def current_table(tables: dict[int, PriceTable]) -> PriceTable:
    """The version in force: the highest recorded version."""
    return tables[max(tables)]


@dataclass
class CostFold:
    """Usage rows folded into money, exactly.

    ``priced_cost`` is a Decimal built only from rows whose price
    was recorded; everything else is counted, with its reason, and
    kept out of the money. ``cost_per_successful_task`` divides the
    priced total by the verified successes the caller counted — the
    webinar's headline metric — and is None when there is nothing
    to divide by yet.
    """

    priced_cost: Decimal = Decimal(0)
    priced_tokens: int = 0
    priced_calls: int = 0
    unpriced_tokens: int = 0
    unpriced_calls: int = 0
    scripted_calls: int = 0
    scripted_chars: int = 0
    price_table_versions: list[int] = field(default_factory=list)
    unpriced_reasons: list[str] = field(default_factory=list)

    def as_dict(self, *, successes: int | None = None) -> dict:
        out: dict = {
            "currency": "USD",
            "priced_cost": str(self.priced_cost),
            "priced_tokens": self.priced_tokens,
            "priced_calls": self.priced_calls,
            "unpriced_tokens": self.unpriced_tokens,
            "unpriced_calls": self.unpriced_calls,
            "scripted_calls": self.scripted_calls,
            "scripted_chars": self.scripted_chars,
            "price_table_versions": list(self.price_table_versions),
            "unpriced_reasons": list(self.unpriced_reasons),
        }
        if successes is not None:
            out["successful_tasks"] = successes
            out["cost_per_successful_task"] = (
                str(self.priced_cost / successes) if successes else None
            )
        return out


def fold_usage(rows: list[dict], tables: dict[int, PriceTable]) -> CostFold:
    """Price usage rows, each under the table version stamped on it.

    A row enters the money only when it is provider accounting
    (source ``api``, unit ``tokens``) AND its stamped table version
    is on disk AND that table records a price for its slug. Every
    other row is counted with its reason:

    - scripted rows are the offline model's own accounting, in
      characters — never priced as tokens, never estimated into
      tokens;
    - a row stamped with a version no longer on disk, or recorded
      before versions were stamped, cannot be repriced honestly;
    - a slug the table leaves unpriced stays unpriced.
    """
    fold = CostFold()
    reasons: set[str] = set()
    versions: set[int] = set()
    for row in rows:
        prompt = int(row["prompt_tokens"])
        completion = int(row["completion_tokens"])
        if row["source"] != "api" or row["unit"] != "tokens":
            fold.scripted_calls += 1
            fold.scripted_chars += prompt + completion
            reasons.add(REASON_SCRIPTED)
            continue
        version = row.get("price_table_version")
        if version is None:
            fold.unpriced_tokens += prompt + completion
            fold.unpriced_calls += 1
            reasons.add(REASON_NO_VERSION)
            continue
        table = tables.get(int(version))
        if table is None:
            fold.unpriced_tokens += prompt + completion
            fold.unpriced_calls += 1
            reasons.add(REASON_VERSION_MISSING)
            continue
        price = table.price_for(row.get("model"))
        if price is None:
            fold.unpriced_tokens += prompt + completion
            fold.unpriced_calls += 1
            reasons.add(REASON_NO_PRICE)
            continue
        input_price, output_price = price
        fold.priced_cost += (
            Decimal(prompt) * input_price + Decimal(completion) * output_price
        ) / _PER_MTOK
        fold.priced_tokens += prompt + completion
        fold.priced_calls += 1
        versions.add(int(version))
    fold.price_table_versions = sorted(versions)
    fold.unpriced_reasons = sorted(reasons)
    return fold
