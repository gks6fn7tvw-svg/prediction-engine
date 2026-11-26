"""Goal mode: automatic bet sizes toward a target."""
import os
import unittest
from decimal import Decimal as D

from btcbot import goal as G
from btcbot.ledger import Ledger
from btcbot.settings import SettingsError, parse

from test_btcbot import BotCase, base_settings


def row(play="main", win=True, price="0.80", n="1", exit_type="settled", profit=None):
    stake = D(n) * D(price) + D("0.02")
    if profit is None:
        profit = (D(n) - stake) if win else -stake
    return {"opened_at": "2026-11-22T02:52:00+00:00", "closed_at": "2026-11-22T03:00:20+00:00",
            "mode": "paper", "play": play, "market": "X", "side": "DOWN", "contracts": n,
            "entry_price": price, "entry_fee": "0.02", "exit_type": exit_type, "exit_price": "1",
            "exit_fee": "0", "result": "DOWN", "payout": "0", "profit": str(profit)}


def record(wins, losses, **kw):
    return [row(win=True, **kw) for _ in range(wins)] + [row(win=False, **kw) for _ in range(losses)]


class TestMath(unittest.TestCase):
    def test_strong_record_is_proven(self):
        e = G.edge(record(36, 4))                      # 90% at 80c (+2c fee)
        self.assertTrue(e.proven)
        self.assertAlmostEqual(e.avg_win, 0.18 / 0.82, places=4)
        self.assertAlmostEqual(e.avg_loss, 1.0, places=4)
        self.assertAlmostEqual(e.cautious_rate, 0.8424, places=3)
        self.assertAlmostEqual(e.kelly, 0.8424 - 0.1576 / (0.18 / 0.82), places=2)   # about 12%
        self.assertEqual(G.bet_share(e, "normal", D(25)), D(str(e.kelly)) / 2)
        self.assertEqual(G.bet_share(e, "bold", D(5)), D("0.05"))                  # capped

    def test_short_or_thin_records_bet_the_smallest_size(self):
        self.assertFalse(G.edge(record(20, 0)).proven)                            # under 30 bets
        main_now = G.edge(record(30, 5))                                          # 86% when ~82% is needed
        self.assertFalse(main_now.proven)
        self.assertLessEqual(main_now.kelly, 0)
        self.assertEqual(G.bet_share(main_now, "bold", D(25)), D(0))
        self.assertIn("doesn't show a clear edge", G.describe(main_now, D(0)))
        self.assertIn("12 of 30", G.describe(G.edge(record(10, 2)), D(0)))

    def test_take_profit_records_use_their_real_wins(self):
        rows = ([row(win=True, price="0.10", n="10", exit_type="take_profit", profit=D("1.40"))] * 20 +
                [row(win=False, price="0.10", n="10")] * 15)
        e = G.edge(rows)                               # wins +1.40 on 1.02 staked, losses -1.02
        self.assertAlmostEqual(e.avg_win, 1.40 / 1.02, places=4)
        self.assertTrue(e.proven)

    def test_contracts(self):
        self.assertEqual(G.contracts_for(D(100), D("0.062"), D("0.80"), D(25)), D(7))
        self.assertEqual(G.contracts_for(D(10), D(0), D("0.80"), D(25)), D(1))   # unproven: 1 contract
        self.assertEqual(G.contracts_for(D(2), D(0), D("0.80"), D(25)), D(0))    # 1 contract is over 25%
        self.assertEqual(G.contracts_for(D(10), D("0.01"), D("0.80"), D(25)), D(1))


class TestSettings(unittest.TestCase):
    def test_goal_settings(self):
        s = parse(base_settings(goal={"enabled": True, "target": 100, "boldness": "careful", "stop_below": 3}))
        self.assertEqual((s.goal.enabled, s.goal.target, s.goal.boldness), (True, D(100), "careful"))
        self.assertFalse(parse(base_settings()).goal.enabled)
        with self.assertRaises(SettingsError):
            parse(base_settings(goal={"enabled": True, "target": 10, "stop_below": 20}))
        with self.assertRaises(SettingsError):
            parse(base_settings(goal={"boldness": "yolo"}))


class TestBot(BotCase):
    def history(self, rows):
        led = Ledger(os.path.join(self.dir, "live"))      # the other mode's log counts too
        for r in rows:
            led.record_trade(r)

    def test_proven_play_bets_its_share(self):
        self.history(record(36, 4))
        self.make(paper_balance=100, balance_share_pct=100,
                  goal={"enabled": True, "target": 1000, "boldness": "normal"})
        self.at(7.5, 0.20, 0.22)                         # Down at 80c
        pos = self.bot.state.open_positions()[0]
        self.assertEqual(D(pos.contracts), D(7))         # about 6% of $100

    def test_unproven_play_bets_one_contract(self):
        self.history(record(30, 5))
        self.make(paper_balance=100, balance_share_pct=100, goal={"enabled": True, "target": 1000})
        self.at(7.5, 0.20, 0.22)
        self.assertEqual(D(self.bot.state.open_positions()[0].contracts), D(1))

    def test_pauses_at_the_goal_and_waits_for_resume(self):
        self.make(paper_balance=10, goal={"enabled": True, "target": 10.10})
        self.at(7.5, 0.20, 0.22)                         # buy Down at 0.80
        self.assertEqual(self.bot.state.goal_start, "10.00")
        self.settle("no")                                # win: about $10.18
        self.assertIn("Goal reached", self.bot.state.goal_note)
        self.assertTrue(os.path.exists(self.bot.stop_file))
        os.remove(self.bot.stop_file)                    # Resume without raising the goal
        self.at(7.5, 0.20, 0.22)
        self.assertTrue(os.path.exists(self.bot.stop_file))   # paused again right away

    def test_stops_below_the_floor(self):
        self.make(paper_balance=10, goal={"enabled": True, "target": 100, "stop_below": 9.5})
        self.at(7.5, 0.20, 0.22)
        self.settle("yes")                               # lose 82c: $9.18
        self.assertIn("fell to $9.18", self.bot.state.goal_note)
        self.assertTrue(os.path.exists(self.bot.stop_file))

    def test_turning_goal_off_clears_it(self):
        self.make(paper_balance=10, goal={"enabled": True, "target": 100})
        self.at(9, 0.20, 0.22)
        self.assertTrue(self.bot.state.goal_start)
        self.bot.s.goal.enabled = False
        self.at(9, 0.20, 0.22)
        self.assertEqual(self.bot.state.goal_start, "")


if __name__ == "__main__":
    unittest.main()
