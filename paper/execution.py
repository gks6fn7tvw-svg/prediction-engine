"""Walk a fixed-point Kalshi book and estimate taker fees for a buy."""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
from typing import Sequence

ZERO = Decimal("0")
ONE = Decimal("1")
WHOLE_CENT = Decimal("0.01")
SUB_CENT = Decimal("0.0001")
FEE_UNIT = Decimal("0.000001")
MIN_FILL_QUANTITY = Decimal("0.01")
TAKER_RATE = Decimal("0.07")
FEE_SCHEDULE_EFFECTIVE_DATE = "2026-07-07"


@dataclass(frozen=True)
class Fill:
    price: Decimal
    quantity: Decimal

    @property
    def notional(self) -> Decimal:
        return self.price * self.quantity


@dataclass(frozen=True)
class BookWalk:
    best_ask: Decimal | None
    quantity_at_limit: Decimal
    fills: tuple[Fill, ...]

    @property
    def notional(self) -> Decimal:
        return sum((fill.notional for fill in self.fills), ZERO)


@dataclass(frozen=True)
class FillFees:
    model_fee: Decimal
    trade_fee: Decimal
    rounding_fee: Decimal
    rebate: Decimal
    rounding_carry: Decimal
    net_fee: Decimal
    total_debit: Decimal


@dataclass(frozen=True)
class BuyFees:
    balance_precision: Decimal
    per_fill: tuple[FillFees, ...]
    net_fee: Decimal
    total_debit: Decimal


def _book_number(value: str, quantum: Decimal) -> Decimal:
    if not isinstance(value, str):
        raise ValueError("book price and quantity must be fixed-point strings")
    try:
        number = Decimal(value)
        normalized = number.quantize(quantum)
    except InvalidOperation as exc:
        raise ValueError("invalid fixed-point number in order book") from exc
    if not number.is_finite() or number != normalized:
        raise ValueError("invalid fixed-point precision in order book")
    return normalized


def walk_buy(
    orderbook: dict,
    *,
    side: str,
    limit: Decimal,
    quantity: Decimal = ONE,
) -> BookWalk:
    """Price a full buy up to the original limit using opposite-side bids."""
    if side not in {"yes", "no"}:
        raise ValueError("side must be yes or no")
    if not limit.is_finite() or not ZERO <= limit <= ONE:
        raise ValueError("limit must be a dollar price from 0 to 1")
    if not quantity.is_finite() or quantity <= ZERO or quantity != quantity.quantize(MIN_FILL_QUANTITY):
        raise ValueError("quantity must be positive in hundredths of a contract")

    opposite = "no_dollars" if side == "yes" else "yes_dollars"
    levels = orderbook.get(opposite)
    if levels is None:
        levels = []
    if not isinstance(levels, list):
        raise ValueError("book side must be an array of price and quantity pairs")

    asks: dict[Decimal, Decimal] = {}
    for level in levels:
        if not isinstance(level, list) or len(level) != 2:
            raise ValueError("book level must contain a price and a quantity")
        bid = _book_number(level[0], SUB_CENT)
        size = _book_number(level[1], MIN_FILL_QUANTITY)
        if not ZERO <= bid <= ONE or size < ZERO:
            raise ValueError("book price or quantity is outside its supported range")
        if size > ZERO:
            ask = ONE - bid
            asks[ask] = asks.get(ask, ZERO) + size

    eligible_asks = sorted((price, size) for price, size in asks.items() if price <= limit)
    available = sum((size for _, size in eligible_asks), ZERO)
    best_ask = min(asks, default=None)
    if available < quantity:
        return BookWalk(best_ask, available, ())

    remaining = quantity
    fills = []
    for price, size in eligible_asks:
        taken = min(remaining, size)
        fills.append(Fill(price, taken))
        remaining -= taken
        if not remaining:
            break
    return BookWalk(best_ask, available, tuple(fills))


def buy_fees(
    fills: Sequence[Fill],
    *,
    multiplier: Decimal,
    balance_precision: Decimal,
) -> BuyFees:
    """Account for one order's fills in order under the general taker schedule."""
    if not multiplier.is_finite() or multiplier < ZERO:
        raise ValueError("fee multiplier must be finite and nonnegative")
    if balance_precision not in {WHOLE_CENT, SUB_CENT}:
        raise ValueError("balance precision must be $0.01 or $0.0001")
    precision = WHOLE_CENT if balance_precision == WHOLE_CENT else SUB_CENT

    carry = ZERO
    fees = []
    for fill in fills:
        notional = fill.notional
        model_fee = TAKER_RATE * multiplier * notional * (ONE - fill.price)
        trade_fee = model_fee.quantize(FEE_UNIT, rounding=ROUND_CEILING)
        # Rounding the positive debit up is equivalent to flooring the signed balance change.
        debit = (notional + trade_fee).quantize(precision, rounding=ROUND_CEILING)
        rounding_fee = debit - notional - trade_fee
        carry += rounding_fee
        fee_before_rebate = trade_fee + rounding_fee
        rebate = min(
            carry.quantize(precision, rounding=ROUND_FLOOR),
            fee_before_rebate.quantize(precision, rounding=ROUND_FLOOR),
        )
        carry -= rebate
        fees.append(FillFees(
            model_fee, trade_fee, rounding_fee, rebate, carry,
            fee_before_rebate - rebate, debit - rebate,
        ))

    return BuyFees(
        precision,
        tuple(fees),
        sum((fill.net_fee for fill in fees), ZERO),
        sum((fill.total_debit for fill in fees), ZERO),
    )


def whole_cent_worst_case(fills: Sequence[Fill], *, multiplier: Decimal) -> BuyFees:
    """Maximize the debit over fill splits along a fixed price ladder."""
    # Splitting a fill can only increase the sum of rounded trade fees and the
    # sum of grid-rounded notionals. Those two amounts determine the cumulative
    # debit with the rebate cap, so the smallest fills attain the worst case.
    fragments = []
    for fill in fills:
        units = fill.quantity / MIN_FILL_QUANTITY
        if not units.is_finite() or units <= ZERO or units != units.to_integral_value():
            raise ValueError("fill quantity must be positive in hundredths of a contract")
        fragments.extend([Fill(fill.price, MIN_FILL_QUANTITY)] * int(units))
    return buy_fees(fragments, multiplier=multiplier, balance_precision=WHOLE_CENT)
