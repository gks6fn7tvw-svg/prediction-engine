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
        headers = self._headers(method, path) if signed else {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
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
    def balance_dollars(self, subaccount: int = 0) -> Decimal:
        params = {"subaccount": subaccount} if subaccount else None
        data = self._request("GET", "/portfolio/balance", params=params, signed=True)
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
        data = self._request("GET", "/portfolio/positions", params=params, signed=True)
        out = {}
        for p in data.get("market_positions", []):
            n = Decimal(str(p.get("position_fp", "0")))
            if p.get("ticker") and n != 0:
                out[p["ticker"]] = out.get(p["ticker"], Decimal("0")) + n
        return out

    def subaccount_balances(self) -> dict[int, Decimal]:
        """Cash in every subaccount (0 = main account), in dollars."""
        data = self._request("GET", "/portfolio/subaccounts/balances", signed=True)
        out: dict[int, Decimal] = {}
        for b in data.get("subaccount_balances", []):
            n = int(b.get("subaccount_number", b.get("subaccount", 0)))
            out[n] = out.get(n, Decimal("0")) + (dollars(b.get("balance")) or Decimal("0"))
        return out

    def create_subaccount(self) -> int:
        """Make the next numbered subaccount (needs the Advanced API level). Returns its number."""
        data = self._request("POST", "/portfolio/subaccounts", body={}, signed=True)
        for key in ("subaccount_number", "subaccount"):
            if data.get(key) is not None:
                return int(data[key])
        raise KalshiError(0, f"Kalshi did not say which subaccount it made: {data}")

    def transfer(self, from_subaccount: int, to_subaccount: int, amount_cents: int,
                 transfer_id: str | None = None) -> dict:
        """Move cash between your own subaccounts. Nothing leaves your Kalshi account."""
        return self._request("POST", "/portfolio/subaccounts/transfer", signed=True, body={
            "client_transfer_id": transfer_id or str(uuid.uuid4()),
            "from_subaccount": int(from_subaccount), "to_subaccount": int(to_subaccount),
            "amount_cents": int(amount_cents)})

    def api_level(self) -> str:
        """basic, advanced, expert, premier, ..."""
        return str(self._request("GET", "/account/limits", signed=True).get("usage_tier", "")).lower()

    def upgrade_api_level(self) -> None:
        """Ask for the free Advanced level (granted once one of the last 100 orders came from the API)."""
        self._request("POST", "/account/api_usage_level/upgrade", signed=True)

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
