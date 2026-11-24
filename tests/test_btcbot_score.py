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
    def test_settings_for_levels_and_buys(self):
        from btcbot.settings import SettingsError, parse
        from test_btcbot import base_settings

        def play(**kw):
            d = dict(DEAL, conditions=dict(DEAL["conditions"]))
            d["conditions"].update(kw.pop("conditions", {}))
            d.update(kw)
            return parse(base_settings(plays=[d])).plays[0]
        self.assertEqual(play().conditions.levels, None)
        self.assertEqual(play(conditions={"levels": "2,3"}).conditions.levels, (2, 3))
        self.assertEqual(play(conditions={"levels": [1, 2, 3]}).conditions.levels, None)
        self.assertEqual((play().max_buys, play(max_buys_per_market=3).max_buys), (1, 3))
        for bad in ({"conditions": {"levels": [4]}}, {"max_buys_per_market": 11}, {"max_buys_per_market": 1.5}):
            with self.assertRaises(SettingsError):
                play(**bad)

    def test_jumpiness_ranks_now_against_the_last_hours(self):
        calm = [60000 + (i % 2) * 10 for i in range(200)]          # $10 moves for hours
        self.assertIsNone(lastminute.jumpiness_from_closes(calm[:20]))      # too little history
        wild = calm + [60060, 60000, 60070, 59990, 60080]           # then five $60-90 moves
        score, dollars = lastminute.jumpiness_from_closes(wild)
        self.assertEqual(score, 100)
        self.assertGreater(dollars, 60)
        quiet = wild + [60080] * 10                                 # then it goes still
        self.assertEqual(lastminute.jumpiness_from_closes(quiet)[0], 1)

    def test_rsi(self):
        from datetime import datetime, timedelta, timezone
        now = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)
        def series(prices):                              # one price a second, ending now
            return [(now - timedelta(seconds=len(prices) - 1 - i), p) for i, p in enumerate(prices)]
        self.assertEqual(lastminute.rsi_series(series([60000] * 30), now), [])       # not enough history yet
        up = lastminute.rsi_series(series([60000 + i for i in range(200)]), now)
        self.assertEqual((up[0][0], up[-1]), (90, (0, 100.0)))                        # 90 s back to now
        self.assertEqual(lastminute.rsi_series(series([60000 - i for i in range(200)]), now)[-1][1], 0.0)
        self.assertEqual(lastminute.rsi_series(series([60000] * 200), now)[-1][1], 50.0)
        zig = [60000 + (10 if (i // 5) % 2 else 0) for i in range(200)]               # up $10, down $10...
        self.assertAlmostEqual(lastminute.rsi_series(series(zig), now)[-1][1], 50, delta=5)

    def test_rsi_guess_carries_the_last_push_a_little(self):
        from datetime import datetime, timedelta, timezone
        now = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)
        rising = [(now - timedelta(seconds=199 - i), 60000 + i) for i in range(200)]
        guess = lastminute.rsi_guess(rising, now)
        self.assertEqual([a for a, _ in guess], [3, 6, 9])
        self.assertTrue(all(r == 100.0 for _, r in guess))           # still only going up
        flat = [(now - timedelta(seconds=199 - i), 60000) for i in range(200)]
        self.assertTrue(all(r == 50.0 for _, r in lastminute.rsi_guess(flat, now)))
        self.assertEqual(lastminute.rsi_guess(rising[-5:], now), [])  # not enough history

    def test_levels(self):
        self.assertEqual([lastminute.confidence(c) for c in (0.5, 0.69, 0.70, 0.89, 0.90, 0.99)], [1, 1, 2, 2, 3, 3])
        self.assertEqual(lastminute.LEVEL_WORDS[3], "about 19 in 20")

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

    def test_shares_the_distance_from_the_target_all_the_time(self):
        self.make([MAIN])
        self.price = D("60040")
        self.run_bot(12, 0.69, 0.70)                     # far from the last minute
        row = self.market_row()
        self.assertEqual((row["btc_now"], row["btc_gap"]), ("60040.00", "40.00"))
        self.bot.sigma_fn = lambda coin: None            # even without our chance
        self.bot.sigmas.clear()
        self.price = D("59990")
        self.run_bot(11, 0.69, 0.70)
        self.assertEqual(self.market_row()["btc_gap"], "-10.00")

    def test_fast_move_colors_the_numbers_for_a_few_seconds(self):
        self.make([MAIN])
        self.price = D("60040")
        self.run_bot(3, 0.69, 0.70, 30)                  # 3:30 left
        self.price = D("60045")
        self.run_bot(3, 0.69, 0.70, 26)                  # +$5 in 4 s: normal
        self.assertNotIn("move", self.market_row())
        self.price = D("60085")
        self.run_bot(3, 0.69, 0.70, 22)                  # +$45 in 8 s on a calm day ($3 a second): up!
        self.assertEqual(self.market_row()["move"], "up")
        self.run_bot(3, 0.69, 0.70, 19)                  # still colored a few seconds later
        self.assertEqual(self.market_row()["move"], "up")
        self.run_bot(3, 0.69, 0.70, 5)                   # quiet again
        self.assertNotIn("move", self.market_row())
        self.price = D("60030")
        self.run_bot(3, 0.69, 0.70, 0)                   # -$55 in 5 s: down
        self.assertEqual(self.market_row()["move"], "down")

    def test_breaking_out_of_a_band(self):
        self.make([MAIN])
        self.bot.sigma_fn = lambda coin: 3.0
        for k in range(60):                              # a minute stuck between $40 and $50 above
            self.price = D("60040") + (k % 3) * 5
            self.run_bot(4, 0.69, 0.70, 30 - k if k < 30 else 0) if k < 30 else self.run_bot(3, 0.69, 0.70, 89 - k)
        self.assertNotIn("move", self.market_row())
        self.price = D("60056")                          # pushes through the top of the band, slowly
        self.run_bot(3, 0.69, 0.70, 29)
        row = self.market_row()
        self.assertEqual((row["move"], row["move_kind"]), ("up", "band"))

    def test_rsi_line_on_the_card(self):
        self.make([MAIN])
        for k in range(36):                              # 3 minutes of a steady climb, a check every 5 s
            self.price = D("60000") + 3 * k
            left = 300 - 5 * k
            self.run_bot(left // 60, 0.69, 0.70, left % 60)
        row = self.market_row()
        self.assertEqual(row["rsi"], 100)
        self.assertEqual(row["rsi_line"][-1], [0, 100])
        self.assertEqual(row["rsi_line"][0][0], 90)
        from btcbot.web import rsi_chart
        html = rsi_chart(row)
        self.assertIn("RSI, last 90 s", html)
        self.assertIn("<polyline", html)
        self.assertIn('cx="250" cy="2"', html)            # now: RSI 100, at the very top
        # target $105 under the price: blue line 105 / 2.5 = 42 px under the dot, prices on the right
        self.assertIn('<line x1="0" y1="44" x2="300" y2="44" stroke="var(--blue)"', html)
        self.assertIn('fill="var(--text)">60,105</text>', html)
        self.assertIn('fill="var(--blue)">60,000</text>', html)
        self.assertIn('stroke="var(--red)" stroke-width="2"', html)     # the 10-second guess
        self.assertEqual([a for a, _ in row["rsi_guess"]], [3, 6, 9])
        self.assertEqual(rsi_chart({"rsi_line": [[0, 50]]}), "")
        # Down winning: target above the dot; a huge gap sticks to the edge
        down = {"rsi_line": [[3, 40], [0, 40]], "rsi": 40, "btc_now": "59970", "target": "60000"}
        self.assertIn('<line x1="0" y1="38" x2="300" y2="38"', rsi_chart(down))     # 50 - 30/2.5
        far = dict(down, btc_now="59000")
        self.assertIn('<line x1="0" y1="2" x2="300" y2="2"', rsi_chart(far))

    def test_rsi_guess_can_be_switched_off(self):
        self.make([MAIN], last_minute={"rsi_guess": False})
        for k in range(36):
            self.price = D("60000") + 3 * k
            left = 300 - 5 * k
            self.run_bot(left // 60, 0.69, 0.70, left % 60)
        row = self.market_row()
        self.assertIn("rsi_line", row)
        self.assertNotIn("rsi_guess", row)

    def test_jumpiness_on_the_card(self):
        self.make([MAIN])
        self.bot.jump_fn = lambda coin: (82, 46.4)
        self.run_bot(4, 0.69, 0.70)
        row = self.market_row()
        self.assertEqual((row["jump"], row["jump_dollars"]), (82, "46"))
        from btcbot.web import jump_meter
        html = jump_meter(row)
        self.assertIn('aria-label="Jumpiness 82 of 100"', html)
        self.assertIn('x2="93.8" y2="38.6"', html)          # needle well into the red part
        self.assertIn('>82</text>', html)
        self.assertEqual(jump_meter({}), "<div></div>")

    def test_level_is_how_sure_the_pick_is(self):
        self.make([MAIN])
        self.price = D("60003")                                          # a hair above: a toss-up
        self.run_bot(5, 0.49, 0.51)
        self.assertEqual(self.market_row()["score_level"], 1)
        self.price = D("60040")                                          # $40 ahead with 4 min left
        self.run_bot(4, 0.69, 0.70)
        self.assertEqual(self.market_row()["score_level"], 2)
        self.price = D("60080")                                          # far ahead near the end
        self.run_bot(1, 0.95, 0.96)
        self.assertEqual(self.market_row()["score_level"], 3)
        self.price = D("59900")                                          # big drop: Down now, and sure
        self.run_bot(0, 0.03, 0.04, 50)
        row = self.market_row()
        self.assertEqual((row["score_best"], row["score_level"]), ("no", 3))

    def test_checks_at_least_every_5_seconds(self):
        self.make([MAIN])
        self.assertLessEqual(self.run_bot(12, 0.50, 0.52), 5)

    def test_switched_off(self):
        self.make([MAIN], last_minute={"score": False})
        self.run_bot(4, 0.69, 0.70)
        self.assertNotIn("score_up", self.market_row())


DEAL = {"name": "deal", "buy": "pick", "min_price": 0.01, "max_price": 0.99,
        "act_from_minutes_left": 5, "act_until_minutes_left": 1, "conditions": {"min_score": 7}}


class TestDealPlay(TestBotScore):
    def test_buys_our_pick_when_it_scores_7_or_more(self):
        self.make([DEAL])
        self.run_bot(4, 0.69, 0.70)                     # $40 ahead: Up is our pick and cheap at 70c
        [pos] = self.bot.state.open_positions()
        self.assertEqual((pos.side, D(pos.entry_price)), ("yes", D("0.70")))

    def test_skips_a_fair_price(self):
        self.make([DEAL])
        self.run_bot(4, 0.85, 0.86)                     # same lead, but Up already costs 86c
        self.assertEqual(self.bot.state.open_positions(), [])
        self.assertIn("UP scores", self.bot.last_reason.get("deal|" + TICKER, ""))

    def test_pick_can_be_the_side_kalshi_has_as_underdog(self):
        self.make([DEAL])
        self.price = D("59960")                         # $40 below: we pick Down
        self.run_bot(4, 0.55, 0.56)                     # Kalshi still has Up ahead, so Down is 45c
        [pos] = self.bot.state.open_positions()
        self.assertEqual(pos.side, "no")

    def test_works_with_the_score_card_off(self):
        self.make([DEAL], last_minute={"score": False})
        self.run_bot(4, 0.69, 0.70)
        self.assertEqual(len(self.bot.state.open_positions()), 1)
        self.assertNotIn("score_up", self.market_row())   # the card stays off; the play still knows our chance

    def test_waits_without_our_chance(self):
        self.make([DEAL])
        self.bot.sigma_fn = lambda coin: None
        self.clock.t = CLOSE - timedelta(minutes=4)
        from test_btcbot import market
        self.client.open = [market(0.69, 0.70)]
        self.bot.spot_fn = lambda product: self.price
        self.bot.step()
        self.assertEqual(self.bot.state.open_positions(), [])


class TestDealExtras(TestBotScore):
    def deal(self, **over):
        d = dict(DEAL, conditions=dict(DEAL["conditions"]))
        for k, v in over.items():
            (d["conditions"] if k == "levels" else d)[k] = v
        return d

    def test_levels_it_buys_at(self):
        self.make([self.deal(levels=[3])])
        self.run_bot(4, 0.69, 0.70)                       # $40 ahead at 4 min: level 2, so no
        self.assertEqual(self.bot.state.open_positions(), [])
        self.assertIn("is level 2 (wanted 3)", self.bot.last_reason.get("deal|" + TICKER, ""))
        self.make([self.deal(levels=[2, 3])])
        self.run_bot(4, 0.69, 0.70)
        self.assertEqual(len(self.bot.state.open_positions()), 1)

    def test_buys_again_up_to_3_times_a_minute_apart(self):
        self.make([self.deal(max_buys_per_market=3)])
        self.run_bot(4, 0.69, 0.70)
        self.run_bot(3, 0.69, 0.70, 30)                   # only 30 seconds later: wait
        self.assertEqual(len(self.bot.state.open_positions()), 1)
        self.run_bot(3, 0.69, 0.70)                       # a minute later and still a deal
        self.run_bot(2, 0.69, 0.70)
        self.run_bot(1, 0.69, 0.70)                       # already 3 buys
        held = self.bot.state.open_positions()
        self.assertEqual([p.side for p in held], ["yes", "yes", "yes"])
        self.assertEqual(len(self.client.orders), 3)

    def test_never_adds_the_other_side(self):
        self.make([self.deal(max_buys_per_market=3)])
        self.run_bot(4, 0.69, 0.70)                       # bought Up
        self.price = D("59960")                           # now we pick Down, and it's cheap
        self.run_bot(3, 0.55, 0.56)
        self.assertEqual([p.side for p in self.bot.state.open_positions()], ["yes"])
        self.assertIn("buying both would cancel out", self.bot.last_reason.get("deal|" + TICKER, ""))

    def test_one_buy_by_default(self):
        self.make([DEAL])
        self.run_bot(4, 0.69, 0.70)
        self.run_bot(3, 0.69, 0.70)
        self.assertEqual(len(self.bot.state.open_positions()), 1)


class TestScorePage(WebCase):
    def test_card_and_track_record(self):
        from btcbot.settings import load, mode_folder
        folder = mode_folder(load(self.path))
        os.makedirs(folder, exist_ok=True)
        close = datetime.now(timezone.utc) + timedelta(minutes=4)
        row = {"ticker": "KXBTC15M-X", "series": "KXBTC15M", "close_time": close.isoformat(), "yes_bid": "0.69",
               "yes_ask": "0.70", "target": "60000", "chance_up": "0.861", "score_up": 9, "score_down": 1,
               "score_best": "yes", "score_level": 2}
        with open(os.path.join(folder, "market.json"), "w") as fh:
            json.dump({"updated": datetime.now(timezone.utc).isoformat(), "markets": [row]}, fh)
        for won in ("yes", "yes", "no"):
            lastminute.append_csv(os.path.join(folder, "scores.csv"), lastminute.SCORE_COLUMNS,
                                  {"best_score": 9, "best_won": won})
        _, body, _ = self.call(f"/?key={KEY}")
        self.assertIn('<span class="score good">9<sup>2</sup></span>▲ Up', body)     # badge on the picked side
        self.assertIn('<div class="odds"><span>Kalshi</span>70%<br><span>Us</span><b>86%</b></div>', body)
        self.assertLess(body.index("▼ Down"), body.index("▲ Up"))                    # Down left, Up right
        self.assertIn("8-10: won 2 of 3", body)
        _, text, _ = self.call("/scores.csv", cookie=f"btcbot_key={KEY}")
        self.assertIn("best_won", text)


if __name__ == "__main__":
    unittest.main()
