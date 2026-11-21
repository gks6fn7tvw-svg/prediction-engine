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

    def record_trade(self, row: dict) -> None:
        new = not os.path.exists(self.trades_path)
        with open(self.trades_path, "a", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=TRADE_COLUMNS)
            if new:
                w.writeheader()
            w.writerow({k: row.get(k, "") for k in TRADE_COLUMNS})


def score(trades_path: str, mode: str | None = None) -> str:
    """Plain-language report card for finished trades."""
    if not os.path.exists(trades_path):
        return "No finished trades yet."
    with open(trades_path, newline="", encoding="utf-8") as fh:
        rows = [r for r in csv.DictReader(fh) if mode is None or r["mode"] == mode]
    if not rows:
        return "No finished trades yet."
    lines = []
    groups = {"ALL PLAYS": rows}
    for r in rows:
        groups.setdefault(r["play"], []).append(r)
    for name, rs in groups.items():
        n = len(rs)
        wins = sum(1 for r in rs if Decimal(r["profit"]) > 0)
        profit = sum((Decimal(r["profit"]) for r in rs), Decimal("0"))
        fees = sum((Decimal(r["entry_fee"] or "0") + Decimal(r["exit_fee"] or "0") for r in rs), Decimal("0"))
        contracts = sum((Decimal(r["contracts"]) for r in rs), Decimal("0"))
        cost = sum((Decimal(r["contracts"]) * Decimal(r["entry_price"]) for r in rs), Decimal("0"))
        avg_price = cost / contracts if contracts else Decimal("0")
        need = (cost + fees) / contracts if contracts else Decimal("0")
        lines += [
            f"== {name} ==",
            f"Trades: {n}   Wins: {wins}   Losses: {n - wins}   Win rate: {100 * wins / n:.1f}%",
            f"Average price paid: {avg_price * 100:.1f}c   Fees paid: ${fees:.2f}",
            f"To break even you need to win about {need * 100:.1f}% of contracts (price + fees).",
            f"Total profit after fees: ${profit:+.2f}",
        ]
        if n < 50:
            lines.append(f"Only {n} trades so far. Wait for about 50 before trusting this.")
        lines.append("")
    return "\n".join(lines)
