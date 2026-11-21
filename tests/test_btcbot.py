"""Offline tests for btcbot: fake Kalshi markets, fake clock, no network."""
import base64
import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

from btcbot import rules
from btcbot.bot import Bot
from btcbot.broker import LiveBroker, PaperBroker
from btcbot.kalshi import KalshiClient, sign
from btcbot.ledger import Ledger, score
from btcbot.settings import SettingsError, parse

T0 = datetime(2026, 11, 21, 20, 0, tzinfo=timezone.utc)
CLOSE = T0 + timedelta(minutes=15)
TICKER = "KXBTC15M-26NOV211515-15"


def base_settings(**over):
    raw = {
        "mode": "paper", "markets": ["KXBTC15M"], "paper_balance": 11,
        "balance_share_pct": 50,
        "size": {"mode": "contracts", "value": 1, "max_contracts": 1},
        "daily_loss_limit": {"mode": "dollars", "value": 5},
        "plays": [{"name": "main", "buy": "favorite", "min_price": 0.70, "max_price": 0.85,
                   "act_from_minutes_left": 8, "act_until_minutes_left": 7}],
    }
    raw.update(over)
    return raw


def market(yes_bid, yes_ask, close=CLOSE, ticker=TICKER, bid_size="50.00", ask_size="50.00", strike=60000.0):
    return {"ticker": ticker, "status": "active", "close_time": close.isoformat().replace("+00:00", "Z"),
            "yes_bid_dollars": f"{yes_bid:.4f}", "yes_ask_dollars": f"{yes_ask:.4f}",
            "yes_bid_size_fp": bid_size, "yes_ask_size_fp": ask_size, "floor_strike": strike}


class FakeClient:
    def __init__(self):
        self.open = []
        self.results = {}
        self.session = None

    def open_markets(self, series):
        return list(self.open)

    def market(self, ticker):
        return {"ticker": ticker, "result": self.results.get(ticker, "")}

    def series(self, series):
        return {"fee_type": "quadratic", "fee_multiplier": 1}


class Clock:
    def __init__(self, t):
        self.t = t

    def now(self):
        return self.t

    def sleep(self, s):
        pass


class BotCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)

    def make(self, **over):
        raw = base_settings(**over)
        raw["files"] = {"folder": self.dir}
        s = parse(raw)
        ledger = Ledger(self.dir)
        state = ledger.load_state(s.paper_balance)
        self.client = FakeClient()
        self.clock = Clock(T0)
        self.broker = PaperBroker(D(state.paper_cash))
        self.bot = Bot(s, self.client, self.broker, ledger, state, self.clock, spot_fn=lambda p: D("60100"))
        self.bot.load_fees()
        return self.bot

    def at(self, minutes_left, yes_bid, yes_ask, **kw):
        self.clock.t = CLOSE - timedelta(minutes=minutes_left)
        self.client.open = [market(yes_bid, yes_ask, **kw)]
        return self.bot.step()

    def settle(self, result):
        self.client.results[TICKER] = result
        self.client.open = []
        self.clock.t = CLOSE + timedelta(seconds=30)
        self.bot.step()

    def trades(self):
        path = os.path.join(self.dir, "trades.csv")
        if not os.path.exists(path):
            return []
        import csv
        with open(path, newline="") as fh:
            return list(csv.DictReader(fh))


class TestFees(unittest.TestCase):
    def test_taker_fee_rounds_up_to_the_cent(self):
        self.assertEqual(rules.taker_fee(D(1), D("0.80")), D("0.02"))   # raw 1.12c
        self.assertEqual(rules.taker_fee(D(1), D("0.90")), D("0.01"))   # raw 0.63c
        self.assertEqual(rules.taker_fee(D(1), D("0.96")), D("0.01"))
        self.assertEqual(rules.taker_fee(D(100), D("0.50")), D("1.75"))
        self.assertEqual(rules.taker_fee(D(10), D("0.50")), D("0.18"))  # 17.5c rounds up


class TestQuote(unittest.TestCase):
    def test_down_prices_come_from_the_up_book(self):
        q = rules.Quote.from_market(market(0.20, 0.22), "KXBTC15M")
        self.assertEqual(rules.favorite(q), "no")
        self.assertEqual(q.ask("no"), D("0.80"))   # buy Down = 1 - Up bid
        self.assertEqual(q.bid("no"), D("0.78"))   # sell Down = 1 - Up ask
        self.assertEqual(q.ask_size("no"), D("50.00"))

    def test_fifty_fifty_has_no_favorite(self):
        q = rules.Quote.from_market(market(0.49, 0.51), "KXBTC15M")
        self.assertIsNone(rules.favorite(q))


class TestEntries(BotCase):
    def test_buys_favorite_inside_window_and_price_range(self):
        self.make()
        self.at(9.0, 0.20, 0.22)            # too early
        self.assertEqual(self.bot.state.positions, [])
        self.at(7.5, 0.20, 0.22)            # Down costs 0.80, in range
        pos = self.bot.state.open_positions()
        self.assertEqual(len(pos), 1)
        self.assertEqual((pos[0].side, pos[0].entry_price, pos[0].entry_fee), ("no", "0.8000", "0.02"))
        self.assertEqual(self.broker.cash, D("11") - D("0.80") - D("0.02"))

    def test_buys_only_once_per_market(self):
        self.make()
        for m in (7.9, 7.6, 7.3, 7.1):
            self.at(m, 0.20, 0.22)
        self.assertEqual(len(self.bot.state.open_positions()), 1)

    def test_skips_when_price_out_of_range(self):
        self.make()
        self.at(7.5, 0.10, 0.12)            # Down costs 0.90 > 0.85
        self.at(7.2, 0.40, 0.45)            # Up mid .425 -> Down favorite costs 0.60 < 0.70
        self.assertEqual(self.bot.state.positions, [])

    def test_up_favorite(self):
        self.make()
        self.at(7.5, 0.76, 0.78)
        pos = self.bot.state.open_positions()[0]
        self.assertEqual((pos.side, pos.entry_price), ("yes", "0.7800"))

    def test_only_buys_what_is_offered(self):
        self.make(size={"mode": "contracts", "value": 5, "max_contracts": 5}, balance_share_pct=100)
        self.at(7.5, 0.76, 0.78, ask_size="2.00")
        self.assertEqual(D(self.bot.state.open_positions()[0].contracts), D(2))

    def test_stop_file_blocks_new_trades(self):
        self.make()
        open(os.path.join(self.dir, "STOP"), "w").close()
        self.at(7.5, 0.20, 0.22)
        self.assertEqual(self.bot.state.positions, [])


class TestSettlement(BotCase):
    def test_win_pays_a_dollar(self):
        self.make()
        self.at(7.5, 0.20, 0.22)
        self.settle("no")
        rows = self.trades()
        self.assertEqual(len(rows), 1)
        self.assertEqual(D(rows[0]["profit"]), D("0.18"))     # 1 - 0.80 - 0.02
        self.assertEqual(rows[0]["result"], "DOWN")
        self.assertEqual(self.broker.cash, D("11.18"))
        self.assertEqual(self.bot.state.positions, [])

    def test_loss_loses_price_and_fee(self):
        self.make()
        self.at(7.5, 0.20, 0.22)
        self.settle("yes")
        self.assertEqual(D(self.trades()[0]["profit"]), D("-0.82"))
        self.assertEqual(self.broker.cash, D("10.18"))

    def test_waits_while_result_is_missing(self):
        self.make()
        self.at(7.5, 0.20, 0.22)
        self.settle("")
        self.assertEqual(len(self.bot.state.positions), 1)
        self.assertEqual(self.trades(), [])

    def test_state_survives_a_restart(self):
        self.make()
        self.at(7.5, 0.20, 0.22)
        ledger = Ledger(self.dir)
        state = ledger.load_state(D(11))
        self.assertEqual(len(state.open_positions()), 1)
        self.assertEqual(D(state.paper_cash), D("10.18"))


class TestExits(BotCase):
    def plays(self, tp=None, sl=None):
        return [{"name": "main", "buy": "favorite", "min_price": 0.70, "max_price": 0.85,
                 "act_from_minutes_left": 8, "act_until_minutes_left": 7, "take_profit": tp, "stop_loss": sl}]

    def test_take_profit_sells_at_the_bid(self):
        self.make(plays=self.plays(tp=0.95))
        self.at(7.5, 0.20, 0.22)            # buy Down at 0.80
        wait = self.at(3.0, 0.02, 0.04)     # Down bid = 0.96
        row = self.trades()[0]
        self.assertEqual((row["exit_type"], D(row["exit_price"])), ("take_profit", D("0.96")))
        self.assertEqual(D(row["profit"]), D("0.96") - D("0.01") - D("0.80") - D("0.02"))
        self.assertEqual(self.bot.state.positions, [])
        self.assertLessEqual(wait, 15)

    def test_stop_loss(self):
        self.make(plays=self.plays(sl=0.50))
        self.at(7.5, 0.20, 0.22)
        self.at(4.0, 0.52, 0.55)            # Down bid = 0.45 <= 0.50
        row = self.trades()[0]
        self.assertEqual(row["exit_type"], "stop_loss")
        self.assertEqual(D(row["exit_price"]), D("0.45"))

    def test_no_exit_when_hold_to_end(self):
        self.make()
        self.at(7.5, 0.20, 0.22)
        self.at(3.0, 0.02, 0.04)
        self.assertEqual(self.trades(), [])


class TestLimits(BotCase):
    def test_daily_loss_limit_stops_new_trades(self):
        self.make(daily_loss_limit={"mode": "dollars", "value": 1})
        self.at(7.5, 0.20, 0.22)
        self.settle("yes")                  # lose 0.82
        second = "KXBTC15M-26NOV211530-30"
        close2 = CLOSE + timedelta(minutes=15)
        self.clock.t = close2 - timedelta(minutes=7.5)
        self.client.open = [market(0.20, 0.22, close=close2, ticker=second)]
        self.bot.step()                     # still allowed: -0.82 > -1
        self.client.results[second] = "yes"
        self.client.open = []
        self.clock.t = close2 + timedelta(seconds=30)
        self.bot.step()                     # now -1.64
        third = "KXBTC15M-26NOV211545-45"
        close3 = close2 + timedelta(minutes=15)
        self.clock.t = close3 - timedelta(minutes=7.5)
        self.client.open = [market(0.20, 0.22, close=close3, ticker=third)]
        self.bot.step()
        self.assertEqual(len(self.trades()), 2)
        self.assertEqual(self.bot.state.positions, [])

    def test_percent_size_and_balance_share(self):
        s = parse(base_settings(size={"mode": "percent", "value": 9, "max_contracts": 0}))
        self.assertEqual(rules.size_contracts(s, D("11"), D("0.80"), D("0")), D(1))   # $0.99 -> 1
        self.assertEqual(rules.size_contracts(s, D("5"), D("0.80"), D("0")), D(0))    # $0.45 -> 0
        s2 = parse(base_settings(size={"mode": "contracts", "value": 10, "max_contracts": 0},
                                 balance_share_pct=50))
        # 50% of (11 + 0) = 5.50 room; 7 x 0.80 is too much, 6 x 0.80 + 0.07 fee fits
        self.assertEqual(rules.size_contracts(s2, D("11"), D("0.80"), D("0")), D(6))
        # with $4 already in open trades: room = 7.5 - 4 = 3.50 -> 4 contracts
        self.assertEqual(rules.size_contracts(s2, D("11"), D("0.80"), D("4")), D(4))


class TestConditions(unittest.TestCase):
    def play(self, **c):
        s = parse(base_settings(plays=[{"name": "p", "buy": "favorite", "min_price": 0.5, "max_price": 0.95,
                                        "act_from_minutes_left": 8, "act_until_minutes_left": 0,
                                        "conditions": c}]))
        return s.plays[0]

    def test_spread(self):
        q = rules.Quote.from_market(market(0.70, 0.76), "KXBTC15M")
        now = CLOSE - timedelta(minutes=5)
        self.assertFalse(rules.check_entry(self.play(max_spread_cents=3), q, now).ok)
        self.assertTrue(rules.check_entry(self.play(max_spread_cents=6), q, now).ok)

    def test_btc_distance_uses_side(self):
        q = rules.Quote.from_market(market(0.70, 0.72, strike=60000.0), "KXBTC15M")
        now = CLOSE - timedelta(minutes=5)
        p = self.play(btc_distance_min_dollars=50)
        self.assertTrue(rules.check_entry(p, q, now, spot=D("60060")).ok)   # Up, $60 past
        self.assertFalse(rules.check_entry(p, q, now, spot=D("60030")).ok)
        self.assertFalse(rules.check_entry(p, q, now, spot=None).ok)

    def test_rising(self):
        q = rules.Quote.from_market(market(0.70, 0.72), "KXBTC15M")
        now = CLOSE - timedelta(minutes=5)
        p = self.play(favorite_rising_seconds=30)
        self.assertTrue(rules.check_entry(p, q, now, [(now - timedelta(seconds=40), D("0.65"))]).ok)
        self.assertFalse(rules.check_entry(p, q, now, [(now - timedelta(seconds=40), D("0.75"))]).ok)
        self.assertFalse(rules.check_entry(p, q, now, [(now - timedelta(seconds=5), D("0.60"))]).ok)


class TestSettings(unittest.TestCase):
    def test_live_needs_confirmation(self):
        with self.assertRaises(SettingsError):
            parse(base_settings(mode="live"))
        s = parse(base_settings(mode="live", live_confirm="I accept real-money risk"))
        self.assertTrue(s.live)

    def test_bad_values(self):
        bad_plays = [
            {"name": "x", "min_price": 0.9, "max_price": 0.8},
            {"name": "x", "act_from_minutes_left": 1, "act_until_minutes_left": 5},
            {"name": "x", "take_profit": 0.6, "stop_loss": 0.7},
            {"name": "x", "buy": "sideways"},
        ]
        for p in bad_plays:
            with self.assertRaises(SettingsError):
                parse(base_settings(plays=[p]))

    def test_example_file_loads(self):
        from btcbot.settings import load
        here = os.path.join(os.path.dirname(__file__), "..", "btcbot", "settings.example.yaml")
        s = load(here)
        self.assertEqual(s.mode, "paper")
        self.assertEqual([p.enabled for p in s.plays], [True, False])


class FakeResponse:
    def __init__(self, status, payload):
        self.status_code = status
        self.payload = payload
        self.content = json.dumps(payload).encode()
        self.text = self.content.decode()

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    def request(self, method, url, params=None, json=None, headers=None, timeout=None):
        self.calls.append({"method": method, "url": url, "params": params, "json": json, "headers": headers})
        return FakeResponse(201, self.reply)


class TestLive(unittest.TestCase):
    def key(self):
        from cryptography.hazmat.primitives.asymmetric import rsa
        return rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def test_signature_is_rsa_pss_over_path_without_query(self):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        key = self.key()
        sig = sign(key, "1700000000000", "GET", "/trade-api/v2/portfolio/positions?ticker=X")
        key.public_key().verify(
            base64.b64decode(sig), b"1700000000000GET/trade-api/v2/portfolio/positions",
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256())  # raises if wrong

    def test_buying_down_sends_a_yes_ask_and_reads_the_fill(self):
        session = FakeSession({"order_id": "o1", "fill_count": "1.00", "remaining_count": "0.00",
                               "average_fill_price": "0.2000", "average_fee_paid": "0.0200", "ts_ms": 1})
        client = KalshiClient(key_id="kid", private_key=self.key(), session=session)
        q = rules.Quote.from_market(market(0.20, 0.22), "KXBTC15M")
        fill = LiveBroker(client).buy(q, "no", D(1), D("0.80"), D(1))
        call = session.calls[0]
        self.assertEqual(call["url"], "https://external-api.kalshi.com/trade-api/v2/portfolio/events/orders")
        body = call["json"]
        self.assertEqual((body["side"], body["price"], body["count"], body["time_in_force"]),
                         ("ask", "0.2000", "1.00", "immediate_or_cancel"))
        self.assertEqual(body["self_trade_prevention_type"], "taker_at_cross")
        self.assertNotIn("reduce_only", body)
        self.assertIn("KALSHI-ACCESS-SIGNATURE", call["headers"])
        self.assertEqual((fill.contracts, fill.price, fill.fee), (D("1.00"), D("0.80"), D("0.0200")))

    def test_selling_down_is_a_reduce_only_yes_bid(self):
        session = FakeSession({"order_id": "o2", "fill_count": "0.00", "remaining_count": "0.00", "ts_ms": 1})
        client = KalshiClient(key_id="kid", private_key=self.key(), session=session)
        q = rules.Quote.from_market(market(0.02, 0.04), "KXBTC15M")
        self.assertIsNone(LiveBroker(client).sell(q, "no", D(1), D("0.96"), D(1)))
        body = session.calls[0]["json"]
        self.assertEqual((body["side"], body["price"], body["reduce_only"]), ("bid", "0.0400", True))

    def test_market_data_is_not_signed(self):
        session = FakeSession({"markets": []})
        KalshiClient(session=session).open_markets("KXBTC15M")
        call = session.calls[0]
        self.assertNotIn("KALSHI-ACCESS-SIGNATURE", call["headers"])
        self.assertEqual(call["params"], {"series_ticker": "KXBTC15M", "status": "open", "limit": 100})


class TestScore(BotCase):
    def test_report(self):
        self.make()
        self.at(7.5, 0.20, 0.22)
        self.settle("no")
        text = score(os.path.join(self.dir, "trades.csv"))
        self.assertIn("Trades: 1", text)
        self.assertIn("Total profit after fees: $+0.18", text)


class FlakyLiveBroker(PaperBroker):
    """Acts live, but its orders lose their reply, like a dropped connection."""
    live = True

    def __init__(self, cash, fail_buy=True, fail_sell=False):
        super().__init__(cash)
        self.fail_buy, self.fail_sell, self.buys = fail_buy, fail_sell, 0

    def buy(self, *a):
        self.buys += 1
        if self.fail_buy:
            raise ConnectionError("reply lost")
        return super().buy(*a)

    def sell(self, *a):
        if self.fail_sell:
            raise ConnectionError("reply lost")
        return super().sell(*a)


class TestLostReplies(BotCase):
    def make_live(self, held, **kw):
        raw = base_settings(mode="live", live_confirm="I accept real-money risk",
                            plays=[{"name": "main", "buy": "favorite", "min_price": 0.70, "max_price": 0.85,
                                    "act_from_minutes_left": 8, "act_until_minutes_left": 7,
                                    "take_profit": 0.95}])
        raw["files"] = {"folder": self.dir}
        s = parse(raw)
        ledger = Ledger(self.dir)
        state = ledger.load_state(D(11))
        self.client = FakeClient()
        self.client.held = held
        self.client.position = lambda ticker: self.client.held
        self.clock = Clock(T0)
        self.broker = FlakyLiveBroker(D(11), **kw)
        self.bot = Bot(s, self.client, self.broker, ledger, state, self.clock)
        return self.bot

    def test_lost_buy_that_filled_is_recorded_once(self):
        self.make_live(held=D("-1"))           # Kalshi shows 1 Down contract
        self.at(7.5, 0.20, 0.22)
        self.at(7.3, 0.20, 0.22)
        pos = self.bot.state.open_positions()
        self.assertEqual(len(pos), 1)
        self.assertEqual((pos[0].side, D(pos[0].contracts), D(pos[0].entry_price)), ("no", D(1), D("0.80")))
        self.assertEqual(self.broker.buys, 1)

    def test_lost_buy_that_did_not_fill_is_not_resent(self):
        self.make_live(held=D("0"))
        self.at(7.5, 0.20, 0.22)
        self.at(7.3, 0.20, 0.22)
        self.assertEqual(self.bot.state.positions, [])
        self.assertEqual(self.broker.buys, 1)

    def test_lost_sell_that_filled_closes_the_trade(self):
        self.make_live(held=D("-1"), fail_buy=False, fail_sell=True)
        self.at(7.5, 0.20, 0.22)               # bought Down for real
        self.client.held = D("0")              # the sale went through, reply lost
        self.at(3.0, 0.02, 0.04)               # take profit fires at 0.96
        row = self.trades()[0]
        self.assertEqual((row["exit_type"], D(row["exit_price"])), ("take_profit", D("0.96")))
        self.assertEqual(self.bot.state.positions, [])

    def test_lost_sell_that_did_not_fill_keeps_the_trade(self):
        self.make_live(held=D("-1"), fail_buy=False, fail_sell=True)
        self.at(7.5, 0.20, 0.22)
        self.at(3.0, 0.02, 0.04)               # Kalshi still shows 1 Down contract
        self.assertEqual(len(self.bot.state.open_positions()), 1)
        self.assertEqual(self.trades(), [])


if __name__ == "__main__":
    unittest.main()
