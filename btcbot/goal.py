"""Goal mode: bet sizes worked out automatically to grow the money toward a target ($10 -> $100).

It uses the Kelly rule: for a bet with a real edge, staking a certain share of your
money each time grows it fastest; staking more than that grows it slower and risks
wiping it out. That share depends on how often the play wins and how much it wins
or loses each time, so it is worked out from the play's own record (paper and real
bets together), counted cautiously:

- the win rate used is the low end of its luck range (a Wilson lower bound),
- plays with fewer than 30 bets, or with no edge at that cautious rate, bet the
  smallest size (1 contract) so they can keep proving themselves,
- "careful", "normal" and "bold" stake 1/4, 1/2 or all of the Kelly share,
- no single bet ever stakes more than the "biggest bet" share of the money.

Nothing here can make a play without a real edge reach the target; it only stops
a good play from betting too little or too much.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal

from . import ledger as L
from .rules import taker_fee
from .settings import BOLDNESS, Settings, mode_folder

MIN_BETS = 30


@dataclass
class Edge:
    bets: int = 0
    wins: int = 0
    win_rate: float = 0.0
    cautious_rate: float = 0.0     # low end of the luck range
    avg_win: float = 0.0           # profit per $1 staked, on wins
    avg_loss: float = 1.0          # loss per $1 staked, on losses
    kelly: float = 0.0             # share of money to stake (cautious); 0 or less = no edge shown
    proven: bool = False

    def growth_per_bet(self, share: float) -> float:
        """Expected log growth per bet at this stake, using the plain win rate."""
        p, q = self.win_rate, 1 - self.win_rate
        if share <= 0 or share * self.avg_loss >= 1:
            return 0.0
        return p * math.log(1 + share * self.avg_win) + q * math.log(1 - share * self.avg_loss)


def wilson_low(wins: int, n: int, z: float = 1.0) -> float:
    if n == 0:
        return 0.0
    p = wins / n
    centre = p + z * z / (2 * n)
    spread = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (centre - spread) / (1 + z * z / n))


def edge(rows: list) -> Edge:
    """How strong a play's record is, from its finished bets."""
    n = len(rows)
    if n == 0:
        return Edge()
    wins, win_returns, loss_returns = 0, [], []
    for r in rows:
        stake = float(r["contracts"]) * float(r["entry_price"]) + float(r["entry_fee"])
        if stake <= 0:
            continue
        profit = float(r["profit"])
        if profit > 0:
            wins += 1
            win_returns.append(profit / stake)
        else:
            loss_returns.append(-profit / stake)
    p = wins / n
    low = wilson_low(wins, n)
    w = sum(win_returns) / len(win_returns) if win_returns else 0.0
    loss = sum(loss_returns) / len(loss_returns) if loss_returns else 1.0  # assume a loss costs the stake
    loss = max(loss, 0.01)
    kelly = (low / loss - (1 - low) / w) if w > 0 else -1.0
    kelly = min(kelly, 1.0)
    return Edge(n, wins, p, low, w, loss, kelly, n >= MIN_BETS and kelly > 0)


def bet_share(e: Edge, boldness: str, max_bet_pct: Decimal) -> Decimal:
    """Share of the money to stake on one bet (0 = no proven edge: bet the smallest size)."""
    if not e.proven:
        return Decimal("0")
    share = Decimal(str(e.kelly)) * BOLDNESS.get(boldness, Decimal("0.5"))
    return max(Decimal("0"), min(share, Decimal(max_bet_pct) / 100))


def contracts_for(money: Decimal, share: Decimal, price: Decimal, max_bet_pct: Decimal) -> Decimal:
    """Whole contracts for this stake, fee included. Unproven plays get 1 contract, if it isn't
    more than the biggest-bet share of the money."""
    if price <= 0 or money <= 0:
        return Decimal("0")
    if share <= 0:
        one = price + taker_fee(Decimal("1"), price)
        return Decimal("1") if one <= money * Decimal(max_bet_pct) / 100 else Decimal("0")
    stake = money * share
    n = (stake / price).to_integral_value(rounding=ROUND_FLOOR)
    while n > 0 and n * price + taker_fee(n, price) > stake:
        n -= 1
    if n == 0:  # a proven play whose share is less than one contract still bets the smallest size
        return contracts_for(money, Decimal("0"), price, max_bet_pct)
    return n


def history_paths(settings: Settings, current_folder: str | None = None) -> list[str]:
    """Trade logs for pretend and real money (the running mode's own folder first)."""
    other = "live" if settings.mode == "paper" else "paper"
    first = current_folder or mode_folder(settings)
    return [os.path.join(first, "trades.csv"), os.path.join(mode_folder(settings, other), "trades.csv")]


def history(settings: Settings, current_folder: str | None = None) -> list:
    """Every finished bet, pretend and real, so a play's record carries over to real money."""
    rows = []
    for path in history_paths(settings, current_folder):
        rows += L.read_trades(path)
    return rows


def describe(e: Edge, share: Decimal) -> str:
    if e.bets < MIN_BETS:
        return f"bets 1 contract until its record proves an edge ({e.bets} of {MIN_BETS} bets so far)"
    if not e.proven:
        return "bets 1 contract: its record doesn't show a clear edge yet"
    return f"bets about {float(share) * 100:.0f}% of its money each time"
