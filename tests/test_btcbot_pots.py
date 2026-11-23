"""Kalshi keeps cash in separate pots ("exchange shards"); crypto markets use pot 2.
API orders can only spend what's already in that pot."""
import json
import os
import unittest
from decimal import Decimal as D

from btcbot.bot import ALLOCATION_FILE
from btcbot.kalshi import KalshiClient, KalshiError
from btcbot.rules import Quote

from test_btcbot import market
from test_btcbot_live import MAIN, FakeExchange, LiveCase
from test_btcbot_web import KEY, WebCase


class CaptureSession:
    def __init__(self, reply=None):
        from test_btcbot import FakeResponse
        self.reply = FakeResponse(200, reply or {})
        self.calls = []

    def request(self, method, url, params=None, json=None, headers=None, timeout=None):
        self.calls.append({"method": method, "url": url, "params": params, "json": json, "headers": headers})
        return self.reply


def signed_client(reply=None):
    from cryptography.hazmat.primitives.asymmetric import rsa
    session = CaptureSession(reply)
    c = KalshiClient(key_id="kid", private_key=rsa.generate_private_key(public_exponent=65537, key_size=2048),
                     session=session)
    c.sleep = lambda s: None
    return c, session


class TestRequests(unittest.TestCase):
    def test_upgrade_sends_a_json_body(self):
        # Kalshi answered 400 invalid_content_type when this POST had no body.
        c, session = signed_client()
        c.upgrade_api_level()
        call = session.calls[0]
        self.assertEqual(call["json"], {})
        self.assertEqual(call["headers"]["Content-Type"], "application/json")

    def test_reads_cash_per_pot(self):
        c, _ = signed_client({"balance": 842, "balance_dollars": "8.4200", "balance_breakdown": [
            {"exchange_index": 0, "balance": "8.4200"}, {"exchange_index": 2, "balance": "0.0000"}]})
        total, pots = c.balance_summary()
        self.assertEqual(total, D("8.42"))
        self.assertEqual(pots, {0: D("8.42"), 2: D("0")})

    def test_one_pot_balance_asks_for_that_pot(self):
        c, session = signed_client({"balance_dollars": "3.0000"})
        self.assertEqual(c.balance_dollars(0, exchange_index=2), D("3"))
        self.assertEqual(session.calls[0]["params"], {"exchange_index": 2})

    def test_keep_money_in_crypto_pot(self):
        c, session = signed_client()
        c.set_target_allocation({2: 100})
        self.assertTrue(session.calls[0]["url"].endswith("/portfolio/target_balance_allocation"))
        self.assertEqual(session.calls[0]["json"], {"allocations": [{"exchange_index": 2, "percent": 100}]})

    def test_market_knows_its_pot(self):
        q = Quote.from_market(dict(market(0.8, 0.82), exchange_index=2), "KXBTC15M")
        self.assertEqual(q.exchange_index, 2)
        self.assertIsNone(Quote.from_market(market(0.8, 0.82), "KXBTC15M").exchange_index)


class PotExchange(FakeExchange):
    """Main account only, cash split into pots. Orders spend from the crypto pot (2)."""

    def __init__(self, general=20, crypto=0):
        super().__init__(subaccounts=(0,), cash=general + crypto)
        self.pots = {0: D(general), 2: D(crypto)}
        self.allocations = []
        self.reject_next = False

    def balance_dollars(self, subaccount=0, exchange_index=None):
        return self.pots.get(exchange_index, D(0)) if exchange_index is not None else sum(self.pots.values())

    def balance_summary(self):
        return sum(self.pots.values()), dict(self.pots)

    def set_target_allocation(self, shares):
        self.allocations.append(dict(shares))
        if shares:
            total = sum(self.pots.values())
            self.pots = {k: (total if k in shares else D(0)) for k in set(self.pots) | set(shares)}

    def place_ioc(self, ticker, yes_side, count, yes_price, reduce_only=False, subaccount=0):
        cost = count * (yes_price if yes_side == "bid" else 1 - yes_price) + count * D("0.01")
        if self.reject_next or cost > self.pots[2]:
            self.reject_next = False
            raise KalshiError(400, '{"error":{"code":"insufficient_balance","message":"insufficient balance"}}')
        self.pots[2] -= cost
        return super().place_ioc(ticker, yes_side, count, yes_price, reduce_only, subaccount)


class TestBotUsesTheCryptoPot(LiveCase):
    def crypto_at(self, minutes_left, yes_bid, yes_ask):
        from datetime import timedelta
        from test_btcbot import CLOSE
        self.clock.t = CLOSE - timedelta(minutes=minutes_left)
        self.client.open = [dict(market(yes_bid, yes_ask), exchange_index=2)]
        return self.bot.step()

    def test_empty_crypto_pot_is_explained_not_tried(self):
        self.make([MAIN], exchange=PotExchange(general=20, crypto=0))
        with self.assertLogs("btcbot", level="INFO") as logs:
            self.crypto_at(7.5, 0.80, 0.82)
        self.assertEqual(self.client.orders, [])
        text = "\n".join(logs.output)
        self.assertIn("crypto money pot", text)
        self.assertIn("Keep my money in the crypto pot", text)

    def test_bets_with_whats_in_the_crypto_pot(self):
        self.make([MAIN], exchange=PotExchange(general=0, crypto=10))
        self.crypto_at(7.5, 0.80, 0.82)
        self.assertEqual(len(self.client.orders), 1)

    def test_kalshi_saying_not_enough_is_explained_and_retried_later(self):
        ex = PotExchange(general=0, crypto=10)
        ex.reject_next = True
        self.make([MAIN], exchange=ex)
        with self.assertLogs("btcbot", level="INFO") as logs:
            self.crypto_at(7.5, 0.80, 0.82)
        self.assertIn("crypto money pot doesn't have enough", "\n".join(logs.output))
        self.assertEqual(self.client.orders, [])
        self.crypto_at(7.4, 0.80, 0.82)          # pot is fine now: next try goes through
        self.assertEqual(len(self.client.orders), 1)

    def test_website_button_asks_kalshi_to_keep_money_in_crypto(self):
        ex = PotExchange(general=20, crypto=0)
        self.make([MAIN], exchange=ex)
        with open(os.path.join(self.ledger.folder, ALLOCATION_FILE), "w") as fh:
            json.dump({"shard": 2}, fh)
        self.crypto_at(7.5, 0.80, 0.82)
        self.assertEqual(ex.allocations, [{2: 100}])
        self.assertIn("crypto pot", self.bot.state.notice)
        self.assertFalse(os.path.exists(os.path.join(self.ledger.folder, ALLOCATION_FILE)))
        self.assertEqual(self.bot.state.shards["0"]["2"], "20")

    def test_turning_it_off(self):
        ex = PotExchange()
        self.make([MAIN], exchange=ex)
        with open(os.path.join(self.ledger.folder, ALLOCATION_FILE), "w") as fh:
            json.dump({"shard": None}, fh)
        self.crypto_at(12, 0.50, 0.52)
        self.assertEqual(ex.allocations, [{}])


class TestWebsitePots(WebCase):
    def test_shows_crypto_pot_and_button(self):
        import yaml
        from btcbot.bot import ALLOCATION_FILE
        from btcbot.ledger import Ledger
        from btcbot.settings import load, mode_folder
        with open(self.path) as fh:
            raw = yaml.safe_load(fh)
        raw.update(mode="live", live_confirm="I accept real-money risk")
        with open(self.path, "w") as fh:
            yaml.safe_dump(raw, fh)
        folder = mode_folder(load(self.path))
        ledger = Ledger(folder)
        state = ledger.load_state(D(0))
        state.accounts = {"0": "8.42"}
        state.shards = {"0": {"0": "8.42", "2": "0"}}
        ledger.save_state(state)
        _, body, _ = self.call(f"/?key={KEY}")
        self.assertIn("$0.00</b> in the crypto pot", body)
        self.assertIn("Keep my money in the crypto pot", body)
        status, _, _ = self.call("/pots", data={"key": KEY, "pot": "2"})
        self.assertEqual(status, 303)
        with open(os.path.join(folder, ALLOCATION_FILE)) as fh:
            self.assertEqual(json.load(fh), {"shard": 2})


if __name__ == "__main__":
    unittest.main()


class TestEmptySubaccounts(LiveCase):
    """Kalshi leaves empty subaccounts out of its list. The bot must not keep making new ones."""

    def test_empty_subaccount_counts_as_set_up(self):
        ex = FakeExchange(subaccounts=(0, 1))          # subaccount 1 exists but holds $0
        self.make([dict(MAIN, subaccount=1)], ex)
        self.assertEqual(getattr(ex, "created", 0), 0)
        self.assertIn(1, self.bot.ready_accounts)
        self.assertEqual(self.bot.state.setup_note, "")
        self.at(12, 0.50, 0.52)
        self.assertEqual(self.bot.state.accounts.get("1"), "0")   # shown as $0, so money can be moved in

    def test_restarts_do_not_make_more(self):
        ex = FakeExchange()
        self.make([dict(MAIN, subaccount=1)], ex)
        self.assertEqual(ex.created, 1)
        for _ in range(3):                              # restarts (each save or update restarts the bot)
            self.make([dict(MAIN, subaccount=1)], ex)
        self.assertEqual(ex.created, 1)
        self.assertEqual(sorted(ex.cash), [0, 1])

    def test_at_most_one_extra_if_kalshi_hides_one(self):
        ex = FakeExchange(subaccounts=(0, 1))
        ex.balance_dollars = lambda subaccount=0: (_ for _ in ()).throw(KalshiError(500, "busy"))
        self.make([dict(MAIN, subaccount=1)], ex)
        self.assertEqual(ex.created, 1)                 # made #2, noticed, stopped
        self.assertIn("subaccount 2", self.bot.state.setup_note)

    def test_money_moves_happen_in_the_crypto_pot(self):
        from btcbot.bot import TRANSFER_FILE
        ex = FakeExchange(subaccounts=(0, 1))
        self.make([dict(MAIN, subaccount=1)], ex)
        with open(os.path.join(self.ledger.folder, TRANSFER_FILE), "w") as fh:
            json.dump({"id": "x", "from": 0, "to": 1, "amount_cents": 500}, fh)
        self.at(12, 0.50, 0.52)
        self.assertEqual(ex.transfer_pot, 2)
        self.assertIn("Moved $5.00", self.bot.state.notice)


class TestMoveMessageShowsByTheButtons(WebCase):
    def test_refusal_is_inside_the_money_box(self):
        import yaml
        from btcbot.ledger import Ledger
        from btcbot.settings import load, mode_folder
        with open(self.path) as fh:
            raw = yaml.safe_load(fh)
        raw.update(mode="live", live_confirm="I accept real-money risk")
        raw["plays"][0]["subaccount"] = 1
        raw["plays"] = raw["plays"][:1]
        with open(self.path, "w") as fh:
            yaml.safe_dump(raw, fh)
        folder = mode_folder(load(self.path))
        ledger = Ledger(folder)
        state = ledger.load_state(D(0))
        state.accounts = {"0": "8.42"}
        ledger.save_state(state)
        self.call("/transfer", data={"key": KEY, "from": "0", "to": "1", "amount": "5"})
        _, body, _ = self.call(f"/?key={KEY}")
        money = body[body.index('id="money"'):]
        self.assertIn("Not moved: subaccount 1 isn", money)
