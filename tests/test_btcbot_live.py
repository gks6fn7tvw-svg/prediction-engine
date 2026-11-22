"""Real-money paths with subaccounts, against a pretend Kalshi exchange (no network)."""
import json
import os
import shutil
import tempfile
import unittest
from datetime import timedelta
from decimal import Decimal as D

from btcbot.bot import Bot, TRANSFER_FILE
from btcbot.broker import LiveBroker
from btcbot.kalshi import KalshiClient, KalshiError
from btcbot.ledger import Ledger
from btcbot.settings import SettingsError, parse

from test_btcbot import CLOSE, T0, TICKER, Clock, FakeClient, FakeSession, base_settings, market

LIVE = {"mode": "live", "live_confirm": "I accept real-money risk"}
MAIN = {"name": "main", "buy": "favorite", "min_price": 0.70, "max_price": 0.85,
        "act_from_minutes_left": 8, "act_until_minutes_left": 7}
CHEAP = {"name": "cheap", "buy": "underdog", "min_price": 0.08, "max_price": 0.20,
         "act_from_minutes_left": 15, "act_until_minutes_left": 11}


class FakeExchange(FakeClient):
    """Keeps cash and positions per subaccount, like Kalshi. Each subaccount holds one net
    position per market (+ Up, - Down), so opposite bets in ONE subaccount cancel out."""

    def __init__(self, level="advanced", subaccounts=(0,), cash=20):
        super().__init__()
        self.level = level
        self.cash = {n: D(cash) if n == 0 else D(0) for n in subaccounts}
        self.pos: dict = {}
        self.orders, self.transfers, self.upgrades = [], [], 0

    # account
    def api_level(self):
        return self.level

    def upgrade_api_level(self):
        self.upgrades += 1
        if not self.orders:
            raise KalshiError(403, "no API-created order in the last 100 orders")
        self.level = "advanced"

    def create_subaccount(self):
        if self.level == "basic":
            raise KalshiError(403, "subaccount_creation_requires_advanced_api_usage_level")
        n = max(self.cash) + 1
        self.cash[n] = D(0)
        return n

    def subaccount_balances(self):
        return dict(self.cash)

    def balance_dollars(self, subaccount=0):
        return self.cash.get(subaccount, D(0))

    def transfer(self, frm, to, cents, transfer_id=None):
        self.transfers.append((frm, to, cents, transfer_id))
        self.cash[frm] -= D(cents) / 100
        self.cash[to] += D(cents) / 100
        return {}

    def position(self, ticker, subaccount=0):
        return self.pos.get((subaccount, ticker), D(0))

    def positions(self, subaccount=0):
        return {t: n for (s, t), n in self.pos.items() if s == subaccount and n != 0}

    # orders fill in full at the limit; fee 1 cent each
    def place_ioc(self, ticker, yes_side, count, yes_price, reduce_only=False, subaccount=0):
        self.orders.append({"side": yes_side, "count": count, "price": yes_price,
                            "reduce_only": reduce_only, "subaccount": subaccount})
        key = (subaccount, ticker)
        cost = count * (yes_price if yes_side == "bid" else 1 - yes_price) + count * D("0.01")
        self.cash[subaccount] = self.cash.get(subaccount, D(0)) - cost
        self.pos[key] = self.pos.get(key, D(0)) + (count if yes_side == "bid" else -count)
        return {"order_id": f"o{len(self.orders)}", "fill_count": str(count), "remaining_count": "0",
                "average_fill_price": str(yes_price), "average_fee_paid": "0.0100", "ts_ms": 1}


class LiveCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)

    def make(self, plays, exchange=None, **over):
        raw = base_settings(plays=plays, balance_share_pct=100, **LIVE, **over)
        raw["files"] = {"folder": self.dir}
        self.s = parse(raw)
        self.ledger = Ledger(self.dir)
        self.client = exchange or FakeExchange()
        self.clock = Clock(T0)
        self.broker = LiveBroker(self.client)
        self.bot = Bot(self.s, self.client, self.broker, self.ledger, self.ledger.load_state(D(0)), self.clock)
        self.bot.prepare_live()
        return self.bot

    def at(self, minutes_left, yes_bid, yes_ask, seconds=0):
        self.clock.t = CLOSE - timedelta(minutes=minutes_left, seconds=seconds)
        self.client.open = [market(yes_bid, yes_ask)]
        return self.bot.step()


class TestClientCalls(unittest.TestCase):
    def client(self, reply):
        from cryptography.hazmat.primitives.asymmetric import rsa
        self.session = FakeSession(reply)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        return KalshiClient(key_id="kid", private_key=key, session=self.session)

    def test_orders_name_their_subaccount(self):
        c = self.client({"order_id": "o", "fill_count": "0", "remaining_count": "0", "ts_ms": 1})
        c.place_ioc(TICKER, "ask", D(1), D("0.20"), subaccount=3)
        c.place_ioc(TICKER, "ask", D(1), D("0.20"))
        self.assertEqual(self.session.calls[0]["json"]["subaccount"], 3)
        self.assertNotIn("subaccount", self.session.calls[1]["json"])   # main account: as before

    def test_positions_and_balance_ask_for_the_subaccount(self):
        c = self.client({"market_positions": [{"ticker": TICKER, "position_fp": "-2.00"},
                                              {"ticker": "OTHER", "position_fp": "0.00"}],
                         "balance": 1234, "balance_dollars": "12.3400"})
        self.assertEqual(c.positions(2), {TICKER: D("-2.00")})
        self.assertEqual(self.session.calls[0]["params"]["subaccount"], 2)
        self.assertEqual(c.position(TICKER, subaccount=2), D("-2.00"))
        self.assertEqual(c.balance_dollars(2), D("12.34"))
        self.assertEqual(self.session.calls[2]["params"], {"subaccount": 2})

    def test_subaccount_setup_calls(self):
        c = self.client({"subaccount_number": 4,
                         "subaccount_balances": [{"subaccount_number": 0, "exchange_index": 0, "balance": "5.5000"},
                                                 {"subaccount_number": 1, "exchange_index": 0, "balance": "2.0000"},
                                                 {"subaccount_number": 1, "exchange_index": 1, "balance": "1.0000"}],
                         "usage_tier": "Basic"})
        self.assertEqual(c.create_subaccount(), 4)
        self.assertEqual(c.subaccount_balances(), {0: D("5.5"), 1: D("3.0")})
        self.assertEqual(c.api_level(), "basic")
        c.transfer(0, 2, 500, "id-1")
        self.assertEqual(self.session.calls[-1]["json"], {"client_transfer_id": "id-1", "from_subaccount": 0,
                                                         "to_subaccount": 2, "amount_cents": 500})
        c.upgrade_api_level()
        self.assertTrue(self.session.calls[-1]["url"].endswith("/account/api_usage_level/upgrade"))


class TestSettingsRule(unittest.TestCase):
    def test_live_plays_need_their_own_subaccounts(self):
        with self.assertRaises(SettingsError):
            parse(base_settings(plays=[MAIN, CHEAP], **LIVE))                # both on subaccount 0
        s = parse(base_settings(plays=[dict(MAIN, subaccount=1), dict(CHEAP, subaccount=2)], **LIVE))
        self.assertEqual([p.subaccount for p in s.plays], [1, 2])
        parse(base_settings(plays=[MAIN, CHEAP]))                           # paper doesn't care
        parse(base_settings(plays=[MAIN, dict(CHEAP, enabled=False)], **LIVE))
        with self.assertRaises(SettingsError):
            parse(base_settings(plays=[dict(MAIN, subaccount=64)]))
        with self.assertRaises(SettingsError):
            parse(base_settings(plays=[dict(MAIN, subaccount=1.5)]))


class TestSubaccounts(LiveCase):
    def test_opposite_bets_stay_apart(self):
        ex = FakeExchange(subaccounts=(0, 1, 2))
        ex.cash[1] = ex.cash[2] = D(5)
        self.make([dict(MAIN, subaccount=1), dict(CHEAP, subaccount=2)], ex)
        self.at(13, 0.85, 0.87)              # Down is the underdog at 15c: cheap buys Down in subaccount 2
        self.at(7.5, 0.80, 0.82)             # Up is the favorite at 82c: main buys Up in subaccount 1
        self.assertEqual(ex.positions(1), {TICKER: D(1)})
        self.assertEqual(ex.positions(2), {TICKER: D(-1)})
        self.assertEqual([o["subaccount"] for o in ex.orders], [2, 1])
        pos = {p.play: p.subaccount for p in self.bot.state.open_positions()}
        self.assertEqual(pos, {"cheap": 2, "main": 1})
        self.at(7.2, 0.80, 0.82, seconds=0)
        self.at(7.0, 0.80, 0.82)             # records check runs: everything matches
        self.assertEqual(self.bot.state.alert, "")
        self.assertFalse(os.path.exists(self.bot.stop_file))

    def test_each_play_sizes_from_its_own_money(self):
        ex = FakeExchange(subaccounts=(0, 1))
        ex.cash[1] = D("0.50")               # not enough for an 82c contract
        self.make([dict(MAIN, subaccount=1)], ex)
        self.at(7.5, 0.80, 0.82)
        self.assertEqual(ex.orders, [])      # main account's $20 is not used for subaccount 1

    def test_sets_up_missing_subaccounts(self):
        ex = FakeExchange(level="basic")
        ex.orders.append({"note": "an earlier API order"})   # qualifies for the free upgrade
        self.make([dict(MAIN, subaccount=1), dict(CHEAP, subaccount=3)], ex)
        self.assertEqual(ex.upgrades, 1)
        self.assertEqual(sorted(ex.cash), [0, 1, 2, 3])
        self.assertEqual(self.bot.ready_accounts, {0, 1, 3})
        self.assertEqual(self.broker.accounts, [0, 1, 3])

    def test_without_the_upgrade_those_plays_wait(self):
        ex = FakeExchange(level="basic")
        self.make([dict(MAIN, subaccount=1)], ex)
        self.assertEqual(self.bot.ready_accounts, {0})
        self.at(7.5, 0.80, 0.82)
        self.assertEqual(ex.orders, [])

    def test_main_account_only_needs_no_setup(self):
        ex = FakeExchange(level="basic")
        self.make([MAIN], ex)
        self.assertEqual((ex.upgrades, sorted(ex.cash)), (0, [0]))
        self.at(7.5, 0.80, 0.82)
        self.assertEqual(ex.positions(0), {TICKER: D(1)})


class TestRecordsCheck(LiveCase):
    def test_pauses_when_kalshi_and_records_disagree(self):
        ex = FakeExchange(subaccounts=(0, 1))
        ex.cash[1] = D(5)
        self.make([dict(MAIN, subaccount=1)], ex)
        self.at(7.5, 0.80, 0.82)                       # bought 1 Up
        ex.pos[(1, TICKER)] = D(3)                     # something else changed the account
        self.at(7.3, 0.80, 0.82)                       # first disagreement: just a warning
        self.assertEqual(self.bot.state.alert, "")
        self.at(7.1, 0.80, 0.82)                       # second one, 10+ seconds later: pause
        self.assertIn("Kalshi shows 3 Up", self.bot.state.alert)
        self.assertIn("records show 1 Up", self.bot.state.alert)
        self.assertTrue(os.path.exists(self.bot.stop_file))
        self.assertIsNotNone(self.bot._entry_block_reason())
        os.remove(self.bot.stop_file)                  # Resume on the website
        ex.pos[(1, TICKER)] = D(1)
        self.at(6.9, 0.80, 0.82)
        self.assertEqual(self.bot.state.alert, "")

    def test_a_one_off_difference_does_not_pause(self):
        ex = FakeExchange(subaccounts=(0, 1))
        ex.cash[1] = D(5)
        self.make([dict(MAIN, subaccount=1)], ex)
        self.at(7.5, 0.80, 0.82)
        ex.pos[(1, TICKER)] = D(0)
        self.at(7.3, 0.80, 0.82)
        ex.pos[(1, TICKER)] = D(1)                     # Kalshi caught up
        self.at(7.1, 0.80, 0.82)
        self.at(6.9, 0.80, 0.82)
        self.assertEqual(self.bot.state.alert, "")

    def test_closed_markets_are_not_compared(self):
        ex = FakeExchange(subaccounts=(0, 1))
        ex.cash[1] = D(5)
        self.make([dict(MAIN, subaccount=1)], ex)
        ex.pos[(1, "KXBTC15M-OLD")] = D(5)             # waiting for Kalshi to settle; not an open market
        self.at(7.5, 0.80, 0.82)
        self.at(7.3, 0.80, 0.82)
        self.at(7.1, 0.80, 0.82)
        self.assertEqual(self.bot.state.alert, "")


class TestMoneyMoves(LiveCase):
    def test_transfer_request_is_done_once(self):
        ex = FakeExchange(subaccounts=(0, 1))
        self.make([dict(MAIN, subaccount=1)], ex)
        with open(os.path.join(self.dir, TRANSFER_FILE), "w") as fh:
            json.dump({"id": "abc", "from": 0, "to": 1, "amount_cents": 500}, fh)
        self.at(9, 0.80, 0.82)
        self.assertEqual(ex.transfers, [(0, 1, 500, "abc")])
        self.assertEqual(ex.cash[1], D(5))
        self.assertIn("Moved $5.00 from your main account to subaccount 1", self.bot.state.notice)
        self.assertFalse(os.path.exists(os.path.join(self.dir, TRANSFER_FILE)))
        self.at(8.9, 0.80, 0.82)
        self.assertEqual(len(ex.transfers), 1)
        self.assertEqual(self.bot.state.accounts.get("1"), "5")

    def test_failed_transfer_says_so(self):
        ex = FakeExchange(subaccounts=(0, 1))
        ex.transfer = lambda *a: (_ for _ in ()).throw(KalshiError(400, "insufficient balance"))
        self.make([dict(MAIN, subaccount=1)], ex)
        with open(os.path.join(self.dir, TRANSFER_FILE), "w") as fh:
            json.dump({"id": "x", "from": 0, "to": 1, "amount_cents": 99999}, fh)
        self.at(9, 0.80, 0.82)
        self.assertIn("Money move failed", self.bot.state.notice)


if __name__ == "__main__":
    unittest.main()
