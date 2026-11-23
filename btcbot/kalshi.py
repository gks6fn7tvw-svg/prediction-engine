"""Talk to Kalshi: public market data, and signed requests for live trading.

Checked against docs.kalshi.com on 2026-11-21:
- Host: https://external-api.kalshi.com/trade-api/v2
- Signing: base64 signature of  timestamp_ms + METHOD + path , where path is the
  full path from the root (it starts with /trade-api/v2) WITHOUT the query string.
  RSA keys use RSA-PSS with SHA-256 (MGF1 SHA-256, salt length = digest length).
  Ed25519 keys sign the same text directly.
- Orders use Create Order V2: POST /portfolio/events/orders. `side` is about the
  YES leg only: "bid" buys YES, "ask" sells YES. Selling YES at (1 - P) is the
  same as buying NO at P.
"""
from __future__ import annotations

import base64
import time
import uuid
from decimal import Decimal, ROUND_DOWN
from urllib.parse import urlparse

import requests

DEFAULT_BASE = "https://external-api.kalshi.com/trade-api/v2"


class KalshiError(Exception):
    def __init__(self, status: int, text: str):
        super().__init__(f"Kalshi HTTP {status}: {text[:300]}")
        self.status = status
        self.text = text


def dollars(value) -> Decimal | None:
    """Turn a Kalshi fixed-point dollar string like "0.5600" into a Decimal."""
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def price_str(p: Decimal) -> str:
    """Kalshi wants 2 to 4 decimals, e.g. "0.5600"."""
    return str(p.quantize(Decimal("0.0001"), rounding=ROUND_DOWN))


def count_str(n: Decimal) -> str:
    return str(Decimal(n).quantize(Decimal("0.01"), rounding=ROUND_DOWN))


def load_private_key(path: str):
    from cryptography.hazmat.primitives import serialization

    with open(path, "rb") as fh:
        return serialization.load_pem_private_key(fh.read(), password=None)


def sign(private_key, timestamp_ms: str, method: str, path: str) -> str:
    """Return the KALSHI-ACCESS-SIGNATURE value for one request."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ed25519, padding

    message = (timestamp_ms + method.upper() + path.split("?")[0]).encode("utf-8")
    if isinstance(private_key, ed25519.Ed25519PrivateKey):
        raw = private_key.sign(message)
    else:
        raw = private_key.sign(
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
    return base64.b64encode(raw).decode("ascii")


class KalshiClient:
    """Small Kalshi REST client. Market data needs no key; trading does."""

    def __init__(self, base_url: str = DEFAULT_BASE, key_id: str | None = None,
                 private_key=None, timeout: float = 10.0, session=None):
        self.base_url = base_url.rstrip("/")
        self.base_path = urlparse(self.base_url).path  # "/trade-api/v2"
        self.key_id = key_id
        self.private_key = private_key
        self.timeout = timeout
        self.session = session or requests.Session()  # keeps the connection open
        self.sleep = time.sleep
        self._last_balance = 0.0

    # ---- plumbing -------------------------------------------------------
    def _headers(self, method: str, path: str) -> dict:
        headers = {"Accept": "application/json"}
        if self.key_id and self.private_key is not None:
            ts = str(int(time.time() * 1000))
            headers.update({
                "KALSHI-ACCESS-KEY": self.key_id,
                "KALSHI-ACCESS-TIMESTAMP": ts,
                "KALSHI-ACCESS-SIGNATURE": sign(self.private_key, ts, method, self.base_path + path),
            })
        return headers

    def _request(self, method: str, path: str, params=None, body=None, signed=False) -> dict:
        if signed and not (self.key_id and self.private_key is not None):
            raise KalshiError(0, "this needs your Kalshi API key, but none is loaded")
        if body is None and method in ("POST", "PUT"):
            body = {}   # Kalshi wants a JSON body (and Content-Type) on every POST
        headers = self._headers(method, path) if signed else {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        resp = self.session.request(method, self.base_url + path, params=params, json=body,
                                    headers=headers, timeout=self.timeout)
        if resp.status_code == 429 and method == "GET":
            # Kalshi's "too many requests": wait a moment and ask once more. Orders are never
            # resent this way (a refused order just didn't happen, and the bot decides what's next).
            self.sleep(1.2)
            headers = self._headers(method, path) if signed else headers
            resp = self.session.request(method, self.base_url + path, params=params, json=body,
                                        headers=headers, timeout=self.timeout)
        if resp.status_code >= 400:
            raise KalshiError(resp.status_code, resp.text)
        return resp.json() if resp.content else {}

    # ---- public market data --------------------------------------------
    def open_markets(self, series_ticker: str) -> list[dict]:
        data = self._request("GET", "/markets", params={
            "series_ticker": series_ticker, "status": "open", "limit": 100})
        return data.get("markets", [])

    def market(self, ticker: str) -> dict:
        return self._request("GET", f"/markets/{ticker}").get("market", {})

    def series(self, series_ticker: str) -> dict:
        return self._request("GET", f"/series/{series_ticker}").get("series", {})

    # ---- account (needs key) --------------------------------------------
    # `subaccount` 0 is the main account; 1-63 are numbered subaccounts, which
    # keep their own money and positions (so two plays can hold opposite sides).
    def _balance_data(self, subaccount: int = 0, exchange_index: int | None = None) -> dict:
        # Kalshi allows about one balance check a second (Basic and Advanced levels).
        wait = 1.1 - (time.monotonic() - self._last_balance)
        if wait > 0:
            self.sleep(wait)
        params = {"subaccount": subaccount} if subaccount else {}
        if exchange_index is not None:
            params["exchange_index"] = exchange_index
        try:
            return self._request("GET", "/portfolio/balance", params=params or None, signed=True)
        finally:
            self._last_balance = time.monotonic()

    def move_between_pots(self, from_shard: int, to_shard: int, amount_dollars: Decimal,
                          transfer_id: str | None = None, subaccount: int = 0) -> str:
        """Move cash from one money pot (exchange shard) to another inside your own account.

        POST /portfolio/intra_exchange_instance_transfer. Kalshi counts this amount in
        centicents ($1 = 10,000), unlike subaccount moves (cents). The move runs in the
        background on Kalshi's side; returns Kalshi's transfer id.
        """
        amount = Decimal(str(amount_dollars)) * 10000
        if not amount.is_finite() or amount <= 0 or amount != amount.to_integral_value():
            raise ValueError(f"can't move ${amount_dollars}")
        body = {"source": "event_contract", "destination": "event_contract",
                "source_exchange_shard": int(from_shard), "destination_exchange_shard": int(to_shard),
                "amount": int(amount)}
        if subaccount:
            body["source_subaccount"] = body["destination_subaccount"] = int(subaccount)
        elif transfer_id:
            body["client_transfer_id"] = transfer_id   # Kalshi accepts this for the main account only
        data = self._request("POST", "/portfolio/intra_exchange_instance_transfer", body=body, signed=True)
        return str(data.get("transfer_id", ""))

    def pot_move(self, transfer_id: str) -> dict:
        """One pot-to-pot move, as Kalshi reports it (has a status)."""
        data = self._request("GET", f"/portfolio/intra_exchange_instance_transfers/{transfer_id}", signed=True)
        return data.get("transfer") or data.get("intra_exchange_instance_transfer") or data

    def shard_balances(self) -> dict[int, Decimal]:
        """Cash per exchange shard, summed over ALL subaccounts (Kalshi's balance_breakdown)."""
        out: dict[int, Decimal] = {}
        for b in self._balance_data().get("balance_breakdown") or []:
            idx = int(b.get("exchange_index", 0))
            out[idx] = out.get(idx, Decimal("0")) + (dollars(b.get("balance")) or Decimal("0"))
        return out

    def target_allocation(self) -> dict[int, int]:
        """How Kalshi is set to spread the money across pots (percent each; {} = off)."""
        data = self._request("GET", "/portfolio/target_balance_allocation", signed=True)
        items = data.get("allocations") or data.get("target_balance_allocation", {}).get("allocations") or []
        return {int(a.get("exchange_index", 0)): int(a.get("percent", 0)) for a in items}

    def set_target_allocation(self, shares: dict[int, int]) -> None:
        """Ask Kalshi to keep the money spread across shards like this (percent each; {} turns it off)."""
        self._request("POST", "/portfolio/target_balance_allocation", signed=True, body={
            "allocations": [{"exchange_index": int(k), "percent": int(v)} for k, v in shares.items()]})

    def balance_dollars(self, subaccount: int = 0, exchange_index: int | None = None) -> Decimal:
        data = self._balance_data(subaccount, exchange_index)
        if data.get("balance_dollars") not in (None, ""):
            return Decimal(str(data["balance_dollars"]))
        return Decimal(int(data.get("balance", 0))) / 100

    def position(self, ticker: str, subaccount: int = 0) -> Decimal:
        """Contracts held in one market: positive = YES, negative = NO."""
        params = {"ticker": ticker}
        if subaccount:
            params["subaccount"] = subaccount
        data = self._request("GET", "/portfolio/positions", params=params, signed=True)
        for p in data.get("market_positions", []):
            if p.get("ticker") == ticker:
                return Decimal(str(p.get("position_fp", "0")))
        return Decimal("0")

    def positions(self, subaccount: int = 0) -> dict[str, Decimal]:
        """Every open (unsettled) position in one subaccount: ticker -> contracts (+ YES, - NO)."""
        params = {"count_filter": "position", "settlement_status": "unsettled", "limit": 1000}
        if subaccount:
            params["subaccount"] = subaccount
        out: dict[str, Decimal] = {}
        for _ in range(20):                         # follow Kalshi's pages (cursor)
            data = self._request("GET", "/portfolio/positions", params=params, signed=True)
            for p in data.get("market_positions", []):
                n = Decimal(str(p.get("position_fp", "0")))
                if p.get("ticker") and n != 0:
                    out[p["ticker"]] = out.get(p["ticker"], Decimal("0")) + n
            if not data.get("cursor"):
                break
            params = dict(params, cursor=data["cursor"])
        return out

    def subaccount_shards(self) -> dict[int, dict[int, Decimal]]:
        """Cash per subaccount, split by money pot: {subaccount: {shard: dollars}}."""
        data = self._request("GET", "/portfolio/subaccounts/balances", signed=True)
        out: dict[int, dict[int, Decimal]] = {}
        for b in data.get("subaccount_balances", []):
            n = int(b.get("subaccount_number", b.get("subaccount", 0)))
            idx = int(b.get("exchange_index", 0))
            pots = out.setdefault(n, {})
            pots[idx] = pots.get(idx, Decimal("0")) + (dollars(b.get("balance")) or Decimal("0"))
        return out

    def subaccount_balances(self, exchange_index: int | None = None) -> dict[int, Decimal]:
        """Cash in every subaccount (0 = main account), in dollars; one shard only if exchange_index is given."""
        data = self._request("GET", "/portfolio/subaccounts/balances", signed=True)
        out: dict[int, Decimal] = {}
        for b in data.get("subaccount_balances", []):
            if exchange_index is not None and int(b.get("exchange_index", 0)) != exchange_index:
                continue
            n = int(b.get("subaccount_number", b.get("subaccount", 0)))
            out[n] = out.get(n, Decimal("0")) + (dollars(b.get("balance")) or Decimal("0"))
        return out

    def create_subaccount(self, exchange_index: int | None = None) -> int:
        """Make the next numbered subaccount (needs the Advanced API level). Returns its number."""
        body = {"exchange_index": exchange_index} if exchange_index is not None else {}
        data = self._request("POST", "/portfolio/subaccounts", body=body, signed=True)
        for key in ("subaccount_number", "subaccount"):
            if data.get(key) is not None:
                return int(data[key])
        raise KalshiError(0, f"Kalshi did not say which subaccount it made: {data}")

    def transfer(self, from_subaccount: int, to_subaccount: int, amount_cents: int,
                 transfer_id: str | None = None, exchange_index: int | None = None) -> dict:
        """Move cash between your own subaccounts, inside one money pot. Nothing leaves your Kalshi account."""
        extra = {"exchange_index": exchange_index} if exchange_index is not None else {}
        return self._request("POST", "/portfolio/subaccounts/transfer", signed=True, body={
            **extra,
            "client_transfer_id": transfer_id or str(uuid.uuid4()),
            "from_subaccount": int(from_subaccount), "to_subaccount": int(to_subaccount),
            "amount_cents": int(amount_cents)})

    def api_level(self) -> str:
        """basic, advanced, expert, premier, ..."""
        return str(self._request("GET", "/account/limits", signed=True).get("usage_tier", "")).lower()

    def upgrade_api_level(self) -> None:
        """Ask for the free Advanced level (granted once one of the last 100 orders came from the API)."""
        # Kalshi refuses a POST without a JSON body ("invalid_content_type"), so send an empty one.
        self._request("POST", "/account/api_usage_level/upgrade", body={}, signed=True)

    def place_ioc(self, ticker: str, yes_side: str, count: Decimal, yes_price: Decimal,
                  reduce_only: bool = False, subaccount: int = 0) -> dict:
        """Place an order that fills right now (up to `count`) or cancels the rest.

        yes_side: "bid" buys YES (or closes NO); "ask" sells YES (or buys NO).
        yes_price: limit price for the YES leg, in dollars.
        """
        body = {
            "ticker": ticker,
            "client_order_id": str(uuid.uuid4()),
            "side": yes_side,
            "count": count_str(count),
            "price": price_str(yes_price),
            "time_in_force": "immediate_or_cancel",
            "self_trade_prevention_type": "taker_at_cross",
        }
        if reduce_only:
            body["reduce_only"] = True
        if subaccount:
            body["subaccount"] = int(subaccount)
        return self._request("POST", "/portfolio/events/orders", body=body, signed=True)


def coinbase_spot(session, product: str, timeout: float = 5.0) -> Decimal | None:
    """Coinbase's last trade price, used only for the optional distance condition.

    Kalshi settles on the CF Benchmarks index (average of the final minute), so
    Coinbase is a close stand-in, not the official number.
    """
    try:
        r = session.get(f"https://api.exchange.coinbase.com/products/{product}/ticker", timeout=timeout)
        if r.status_code != 200:
            return None
        return Decimal(str(r.json()["price"]))
    except Exception:
        return None
