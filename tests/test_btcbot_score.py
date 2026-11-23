"""Live 1-10 score for each side: our chance vs Kalshi's price."""
import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

from btcbot import lastminute
from btcbot.lastminute import chance_up, score

from test_btcbot import CLOSE, TICKER
from test_btcbot_live import MAIN, LiveCase
from test_btcbot_web import KEY, WebCase


class TestMath(unittest.TestCase):
    def test_chance(self):
        self.assertAlmostEqual(chance_up(60000, 60000, 300, 5), 0.5, places=3)
        calm, jumpy = chance_up(60040, 60000, 300, 3), chance_up(60040, 60000, 300, 10)
        self.assertGreater(calm, jumpy)                       # same $40 lead is safer on a calm day
        self.assertGreater(jumpy, 0.5)
        self.assertGreater(chance_up(60020, 60000, 10, 8), 0.99)   # most of the average already counted

    def test_score(self):
        self.assertEqual(score(0.70, 0.68), 5)                # fair (the fee eats the 2 points)
        self.assertGreaterEqual(score(0.85, 0.70), 8)         # cheap for its chance
        self.assertLessEqual(score(0.55, 0.70), 2)            # expensive
        self.assertIsNone(score(0.5, None))

    def test_summary(self):
        rows = [{"best_score": "9", "best_won": "yes"}, {"best_score": "8", "best_won": "no"},
                {"best_score": "6", "best_won": "yes"}, {"best_score": "3", "best_won": "no"}]
        self.assertEqual(lastminute.score_summary(rows), {"8-10": (1, 2), "6-7": (1, 1), "1-5": (0, 1)})


class TestBotScore(LiveCase):
    def setUp(self):
        super().setUp()
        self.price = D("60040")

    def run_bot(self, minutes_left, yes_bid, yes_ask, seconds=0):
        self.bot.spot_fn = lambda product: self.price
        self.bot.index_fn = lambda coin: (self.price, 3)
        self.bot.sigma_fn = lambda coin: 3.0                 # a calm day: about $3 a second
        return self.at(minutes_left, yes_bid, yes_ask, seconds)

    def market_row(self):
        with open(os.path.join(self.ledger.folder, "market.json")) as fh:
            return json.load(fh)["markets"][0]

    def test_score_shows_best_side_and_changes_with_time(self):
        self.make([MAIN])
        self.run_bot(4, 0.69, 0.70)                           # $40 ahead, Up only 70c
        row = self.market_row()
        self.assertEqual(row["score_best"], "yes")
        early = row["score_up"]
        self.assertGreaterEqual(early, 7)
        self.run_bot(2, 0.69, 0.70)                           # same lead, less time: even surer
        self.assertGreaterEqual(self.market_row()["score_up"], early)
        self.assertLessEqual(self.market_row()["score_down"], 2)

    def test_snapshots_get_their_results(self):
        self.make([MAIN])
        for minutes, sec in ((5, 0), (4, 0), (3, 0), (2, 0), (1, 0), (0, 30)):
            self.run_bot(minutes, 0.69, 0.70, sec)
        self.client.results[TICKER] = "yes"
        self.run_bot(-0.2, 0.99, 1.0)
        with open(os.path.join(self.ledger.folder, "scores.csv")) as fh:
            import csv
            rows = list(csv.DictReader(fh))
        self.assertEqual([int(r["seconds_left"]) for r in rows], [300, 240, 180, 120, 60, 30])
        self.assertTrue(all(r["best"] == "UP" and r["best_won"] == "yes" for r in rows))
        self.assertEqual(self.bot.state.score_pending, {})

    def test_checks_at_least_every_5_seconds(self):
        self.make([MAIN])
        self.assertLessEqual(self.run_bot(12, 0.50, 0.52), 5)

    def test_switched_off(self):
        self.make([MAIN], last_minute={"score": False})
        self.run_bot(4, 0.69, 0.70)
        self.assertNotIn("score_up", self.market_row())


class TestScorePage(WebCase):
    def test_card_and_track_record(self):
        from btcbot.settings import load, mode_folder
        folder = mode_folder(load(self.path))
        os.makedirs(folder, exist_ok=True)
        close = datetime.now(timezone.utc) + timedelta(minutes=4)
        row = {"ticker": "KXBTC15M-X", "series": "KXBTC15M", "close_time": close.isoformat(), "yes_bid": "0.69",
               "yes_ask": "0.70", "target": "60000", "chance_up": "0.861", "score_up": 9, "score_down": 1,
               "score_best": "yes"}
        with open(os.path.join(folder, "market.json"), "w") as fh:
            json.dump({"updated": datetime.now(timezone.utc).isoformat(), "markets": [row]}, fh)
        for won in ("yes", "yes", "no"):
            lastminute.append_csv(os.path.join(folder, "scores.csv"), lastminute.SCORE_COLUMNS,
                                  {"best_score": 9, "best_won": won})
        _, body, _ = self.call(f"/?key={KEY}")
        self.assertIn("Score now: Up 9/10</b> (our chance 86%)", body)
        self.assertIn("8-10: won 2 of 3", body)
        _, text, _ = self.call("/scores.csv", cookie=f"btcbot_key={KEY}")
        self.assertIn("best_won", text)


if __name__ == "__main__":
    unittest.main()
