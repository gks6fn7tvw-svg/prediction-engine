"""Pretend-money-only plays running beside real-money plays."""
import json
import os
import shutil
import tempfile
import unittest
from datetime import timedelta
from decimal import Decimal as D

from btcbot.bot import Bot, PAPER_RESET_FILE, run_together
from btcbot.broker import LiveBroker, PaperBroker
from btcbot.ledger import Ledger
from btcbot.settings import SettingsError, parse, split_pretend

from test_btcbot import CLOSE, T0, TICKER, Clock, base_settings, market
from test_btcbot_live import CHEAP, LIVE, MAIN, FakeExchange
from test_btcbot_web import KEY, WebCase, good_form

PRETEND_CHEAP = dict(CHEAP, pretend=True)


class TestSettings(unittest.TestCase):
    def test_pretend_plays_may_share_a_subaccount(self):
        s = parse(base_settings(plays=[MAIN, PRETEND_CHEAP], **LIVE))       # both on subaccount 0
        self.assertTrue(s.plays[1].pretend)
        with self.assertRaises(SettingsError):
            parse(base_settings(plays=[MAIN, CHEAP], **LIVE))

    def test_split(self):
        s = parse(base_settings(plays=[MAIN, PRETEND_CHEAP], **LIVE))
        real, fake = split_pretend(s)
        self.assertEqual([p.enabled for p in real.plays], [True, False])
        self.assertEqual([p.enabled for p in fake.plays], [False, True])
        self.assertEqual((real.mode, fake.mode), ("live", "paper"))
        self.assertFalse(fake.watch.enabled)
        self.assertIsNone(split_pretend(parse(base_settings(plays=[MAIN], **LIVE)))[1])
        self.assertIsNone(split_pretend(parse(base_settings(plays=[MAIN, PRETEND_CHEAP])))[1])   # paper mode


class TestRunTogether(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        raw = base_settings(plays=[MAIN, PRETEND_CHEAP], balance_share_pct=100, **LIVE)
        raw["files"] = {"folder": self.dir}
        s = parse(raw)
        real_s, fake_s = split_pretend(s)
        self.client = FakeExchange()
        self.clock = Clock(T0)
        self.real_ledger, self.fake_ledger = Ledger(os.path.join(self.dir, "live")), Ledger(os.path.join(self.dir, "paper"))
        self.real = Bot(real_s, self.client, LiveBroker(self.client), self.real_ledger,
                        self.real_ledger.load_state(D(0)), self.clock)
        self.real.prepare_live()
        fstate = self.fake_ledger.load_state(D(11))
        self.fake = Bot(fake_s, self.client, PaperBroker(D(fstate.paper_cash)), self.fake_ledger, fstate, self.clock)
        self.fake.stop_file = self.real.stop_file

    def run_once(self, minutes_left, yes_bid, yes_ask):
        self.clock.t = CLOSE - timedelta(minutes=minutes_left)
        self.client.open = [market(yes_bid, yes_ask)]
        runs = iter([False, True])
        run_together(self.real, self.fake, lambda: next(runs))

    def test_pretend_play_never_sends_an_order(self):
        with self.assertLogs("btcbot", level="INFO") as logs:
            self.run_once(12, 0.80, 0.82)            # the cheap play's window: buys Down at about 20c
        self.assertEqual(self.client.orders, [])     # nothing sent to Kalshi
        held = self.fake.state.open_positions()
        self.assertEqual([(p.play, p.side) for p in held], [("cheap", "no")])
        self.assertEqual(self.real.state.open_positions(), [])
        self.assertTrue(any("pretend money · cheap: BOUGHT" in line for line in logs.output))

    def test_real_play_still_uses_kalshi(self):
        self.run_once(7.5, 0.80, 0.82)               # main's window: real order
        self.assertEqual(len(self.client.orders), 1)
        self.assertEqual(self.fake.state.open_positions(), [])

    def test_pause_stops_both(self):
        open(self.real.stop_file, "w").close()
        self.run_once(12, 0.80, 0.82)
        self.assertEqual(self.fake.state.open_positions(), [])


class TestWebsite(WebCase):
    def live_with_pretend(self):
        import yaml
        from btcbot.settings import load, mode_folder
        with open(self.path) as fh:
            raw = yaml.safe_load(fh)
        raw.update(mode="live", live_confirm="I accept real-money risk")
        raw["plays"] = [dict(MAIN), dict(PRETEND_CHEAP)]
        with open(self.path, "w") as fh:
            yaml.safe_dump(raw, fh)
        s = load(self.path)
        return mode_folder(s, "live"), mode_folder(s, "paper")

    def test_pretend_play_shows_its_pretend_bets_only(self):
        live, paper = self.live_with_pretend()
        real, fake = Ledger(live), Ledger(paper)
        row = {"opened_at": "2026-11-23T00:00:00+00:00", "closed_at": "2026-11-23T00:10:00+00:00",
               "market": TICKER, "side": "UP", "contracts": "1", "entry_price": "0.80", "entry_fee": "0.01",
               "exit_type": "settled", "exit_price": "1", "exit_fee": "0", "result": "UP", "payout": "1"}
        real.record_trade(dict(row, mode="live", play="main", profit="0.19"))
        fake.record_trade(dict(row, mode="paper", play="cheap", profit="5.00"))
        fake.record_trade(dict(row, mode="paper", play="main", profit="9.00"))     # old pretend bet: not shown
        st = fake.load_state(D(11))
        st.positions = [{"play": "cheap", "ticker": TICKER, "side": "no", "contracts": "3", "entry_price": "0.12",
                         "entry_fee": "0.01", "opened_at": "2026-11-23T00:00:00+00:00",
                         "close_time": (CLOSE).isoformat()}]
        st.paper_cash = "7.25"
        fake.save_state(st)
        _, body, _ = self.call(f"/?key={KEY}")
        self.assertIn("Pretend money only", body)
        self.assertIn("$7.25 left", body)
        self.assertIn("cheap (pretend)", body)                 # its open bet, marked
        self.assertIn("+$0.19", body)                          # all-time = real money only
        self.assertNotIn("+$14.19", body)
        _, body, _ = self.call("/play?name=cheap", cookie=f"btcbot_key={KEY}")
        self.assertIn("+$5.00", body)

    def test_switch_saves_and_reset_goes_to_pretend_books(self):
        live, paper = self.live_with_pretend()
        form = dict(good_form(), action="save")
        form["p0_pretend"] = "1"
        self.call("/settings", data=form)
        from btcbot.settings import load
        self.assertTrue(load(self.path).plays[0].pretend)
        _, body, _ = self.call("/settings", cookie=f"btcbot_key={KEY}")
        self.assertIn('name="p0_pretend" value="1" checked', body)
        self.call("/settings", data=dict(form, action="reset_paper"))
        self.assertTrue(os.path.exists(os.path.join(paper, PAPER_RESET_FILE)))
        self.assertFalse(os.path.exists(os.path.join(live, PAPER_RESET_FILE)))


if __name__ == "__main__":
    unittest.main()
