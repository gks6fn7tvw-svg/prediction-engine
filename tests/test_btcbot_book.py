"""Live order-book prices: Kalshi's market list lags about 30 seconds, the book doesn't."""
import unittest
from datetime import timedelta
from decimal import Decimal as D

from btcbot.kalshi import KalshiClient, KalshiError
from tests.test_btcbot import CLOSE, TICKER, BotCase, FakeClient, FakeSession


class BookClient(FakeClient):
    def __init__(self, book=None, error=None):
        super().__init__()
        self.book = book
        self.error = error
        self.asked = []

    def best_prices(self, ticker):
        self.asked.append(ticker)
        if self.error:
            raise self.error
        return self.book


def book(yes_bid, yes_ask):
    return {"yes_bid": D(str(yes_bid)), "yes_ask": D(str(yes_ask)), "yes_bid_size": D(5), "yes_ask_size": D(5)}


class TestReadBook(unittest.TestCase):
    def test_best_prices_from_bids_only(self):
        session = FakeSession({"orderbook_fp": {
            "yes_dollars": [["0.5800", "10.00"], ["0.6200", "3.00"]],
            "no_dollars": [["0.3000", "4.00"], ["0.3600", "7.00"]]}})
        b = KalshiClient(session=session).best_prices(TICKER)
        self.assertEqual((b["yes_bid"], b["yes_ask"]), (D("0.62"), D("0.64")))   # 1 - 0.36
        self.assertEqual((b["yes_bid_size"], b["yes_ask_size"]), (D("3.00"), D("7.00")))
        self.assertTrue(session.calls[0]["url"].endswith(f"/markets/{TICKER}/orderbook"))

    def test_empty_side_and_old_cents_format(self):
        b = KalshiClient(session=FakeSession({"orderbook": {"yes": [[40, 2]], "no": None}})).best_prices(TICKER)
        self.assertEqual((b["yes_bid"], b["yes_ask"]), (D("0.40"), None))


class TestBotUsesBook(BotCase):
    def setUp(self):
        super().setUp()
        self.make()

    def use(self, client):
        client.open = self.client.open
        self.client = self.bot.client = client

    def test_live_price_wins_over_the_list(self):
        # The list still says Down costs 80c (in main's 70-85c range), but it has already jumped to 95c.
        self.use(BookClient(book(0.04, 0.05)))
        self.at(7.5, 0.20, 0.22)
        self.assertEqual(self.client.asked, [TICKER])
        self.assertEqual(self.bot.state.open_positions(), [])

    def test_buys_at_the_live_price(self):
        self.use(BookClient(book(0.23, 0.25)))   # list: Down 80c; book: Down 77c
        self.at(7.5, 0.20, 0.22)
        [pos] = self.bot.state.open_positions()
        self.assertEqual(D(pos.entry_price), D("0.77"))

    def test_refused_book_falls_back_to_the_list_and_stops_asking(self):
        self.use(BookClient(error=KalshiError(401, "no")))
        self.at(7.5, 0.20, 0.22)
        self.assertEqual(len(self.bot.state.open_positions()), 1)
        self.at(7.4, 0.20, 0.22)
        self.assertEqual(len(self.client.asked), 1)
        self.assertFalse(self.bot.book_ok)

    def test_other_errors_keep_trying(self):
        self.use(BookClient(error=KalshiError(500, "busy")))
        self.at(7.5, 0.20, 0.22)
        self.at(7.4, 0.20, 0.22)
        self.assertEqual(len(self.client.asked), 2)
        self.assertTrue(self.bot.book_ok)

    def test_markets_far_from_closing_skip_the_book(self):
        self.use(BookClient(book(0.04, 0.05)))
        self.clock.t = CLOSE - timedelta(minutes=20)
        from tests.test_btcbot import market
        self.client.open = [market(0.20, 0.22)]
        self.bot.step()
        self.assertEqual(self.client.asked, [])


if __name__ == "__main__":
    unittest.main()
