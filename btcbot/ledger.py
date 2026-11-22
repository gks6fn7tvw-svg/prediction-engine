"""Remember open trades between restarts, log finished trades, and score them."""
from __future__ import annotations

import csv
import json
import os
from dataclasses import asdict, dataclass, field
from decimal import Decimal

TRADE_COLUMNS = [
    "opened_at", "closed_at", "mode", "play", "market", "side", "contracts",
    "entry_price", "entry_fee", "exit_type", "exit_price", "exit_fee",
    "result", "payout", "profit",
]


@dataclass
class Position:
    play: str
    ticker: str
    series: str
    side: str               # "yes" (Up) or "no" (Down)
    contracts: str          # Decimal stored as text so JSON keeps it exact
    entry_price: str
    entry_fee: str
    opened_at: str
    close_time: str
    subaccount: int = 0     # Kalshi subaccount holding it (real money only)

    def d(self, name: str) -> Decimal:
        return Decimal(getattr(self, name))

    @property
    def cost(self) -> Decimal:
        return self.d("contracts") * self.d("entry_price")


@dataclass
class State:
    paper_cash: str = "0"
    positions: list = field(default_factory=list)
    entered: list = field(default_factory=list)   # "play|ticker" already bought
    attempts: dict = field(default_factory=dict)  # "play|ticker" -> order tries
    day: str = ""
    day_start_balance: str = "0"
    day_profit: str = "0"
    day_trades: int = 0
    alert: str = ""                               # why the bot paused itself (shown in red)
    notice: str = ""                              # last money-move result
    accounts: dict = field(default_factory=dict)  # real money: subaccount -> cash, for the website

    def open_positions(self) -> list[Position]:
        return [Position(**p) for p in self.positions]

    def set_positions(self, positions: list[Position]) -> None:
        self.positions = [asdict(p) for p in positions]


class Ledger:
    def __init__(self, folder: str):
        self.folder = folder
        os.makedirs(folder, exist_ok=True)
        self.state_path = os.path.join(folder, "state.json")
        self.trades_path = os.path.join(folder, "trades.csv")

    def load_state(self, paper_balance: Decimal) -> State:
        if os.path.exists(self.state_path):
            with open(self.state_path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            return State(**{k: v for k, v in raw.items() if k in State.__dataclass_fields__})
        return State(paper_cash=str(paper_balance))

    def save_state(self, state: State) -> None:
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(asdict(state), fh, indent=2)
        os.replace(tmp, self.state_path)  # never leaves a half-written file

    def save_market(self, now, rows: list) -> None:
        """Latest prices, for the website."""
        path = os.path.join(self.folder, "market.json")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"updated": now.isoformat(), "markets": rows}, fh)
        os.replace(tmp, path)

    def record_trade(self, row: dict) -> None:
        new = not os.path.exists(self.trades_path)
        with open(self.trades_path, "a", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=TRADE_COLUMNS)
            if new:
                w.writeheader()
            w.writerow({k: row.get(k, "") for k in TRADE_COLUMNS})


def read_trades(trades_path: str, mode: str | None = None) -> list[dict]:
    if not os.path.exists(trades_path):
        return []
    with open(trades_path, newline="", encoding="utf-8") as fh:
        return [r for r in csv.DictReader(fh) if mode is None or r["mode"] == mode]


def stats(rows: list[dict]) -> dict:
    """Numbers for a group of finished trades (see score() for the words)."""
    n = len(rows)
    wins = sum(1 for r in rows if Decimal(r["profit"]) > 0)
    profit = sum((Decimal(r["profit"]) for r in rows), Decimal("0"))
    fees = sum((Decimal(r["entry_fee"] or "0") + Decimal(r["exit_fee"] or "0") for r in rows), Decimal("0"))
    contracts = sum((Decimal(r["contracts"]) for r in rows), Decimal("0"))
    cost = sum((Decimal(r["contracts"]) * Decimal(r["entry_price"]) for r in rows), Decimal("0"))
    rate = wins / n if n else 0.0
    # Win rate needed to break even. Held to the end, a contract pays $1 or 0,
    # so it is the price paid plus fees. Plays that sell early win and lose
    # other amounts, so there it comes from the average win and average loss.
    need = float((cost + fees) / contracts) if contracts else 0.0
    won = [Decimal(r["profit"]) for r in rows if Decimal(r["profit"]) > 0]
    lost = [-Decimal(r["profit"]) for r in rows if Decimal(r["profit"]) <= 0]
    if won and lost and any(r["exit_type"] != "settled" for r in rows):
        avg_win, avg_loss = sum(won) / len(won), sum(lost) / len(lost)
        need = float(avg_loss / (avg_win + avg_loss)) if avg_win + avg_loss > 0 else need
    luck = luck_margin(rate, n) if n else 0.0
    return {
        "trades": n, "wins": wins, "losses": n - wins, "win_rate": rate, "profit": profit,
        "fees": fees, "avg_price": (cost / contracts) if contracts else Decimal("0"),
        "need": need, "luck": luck, "verdict": verdict(rate, need, luck, n) if n else "No finished trades yet.",
    }


def score(trades_path: str, mode: str | None = None) -> str:
    """Plain-language report card for finished trades."""
    rows = read_trades(trades_path, mode)
    if not rows:
        return "No finished trades yet."
    lines = []
    groups = {"ALL PLAYS": rows}
    for r in rows:
        groups.setdefault(r["play"], []).append(r)
    for name, rs in groups.items():
        st = stats(rs)
        n = st["trades"]
        lines += [
            f"== {name} ==",
            f"Trades: {n}   Wins: {st['wins']}   Losses: {st['losses']}   Win rate: {100 * st['win_rate']:.1f}%",
            f"Average price paid: {st['avg_price'] * 100:.1f}c   Fees paid: ${st['fees']:.2f}",
            f"To break even it needs to win about {st['need'] * 100:.1f}% of trades.",
            f"Total profit after fees: ${st['profit']:+.2f}",
            f"Luck can move a win rate this much with {n} trades: about +/-{100 * st['luck']:.1f} points.",
            st["verdict"],
        ]
        lines.append("")
    return "\n".join(lines)


MIN_TRADES = 200  # first checkpoint; more trades shrink the luck range


def luck_margin(rate: float, n: int) -> float:
    """About 95% of the time, pure luck stays inside this many points of the true win rate."""
    p = min(max(rate, 0.02), 0.98)  # never let the range shrink to zero
    return 2 * (p * (1 - p) / n) ** 0.5


def verdict(rate: float, need: float, luck: float, n: int) -> str:
    if n < MIN_TRADES:
        return f"Verdict: too early. {n} of {MIN_TRADES} trades so far. Keep it running."
    if rate - luck > need:
        return "Verdict: ahead by more than luck usually explains. A good sign; keep watching."
    if rate + luck < need:
        return "Verdict: behind by more than bad luck explains. This setting is losing money."
    return "Verdict: too close to call. Keep it running; more trades shrink the luck range."
