"""Replay one caller-selected candidate against a saved cutoff capture."""

import hashlib
import json
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from .execution import (
    FEE_SCHEDULE_EFFECTIVE_DATE,
    MIN_FILL_QUANTITY,
    ONE,
    SUB_CENT,
    WHOLE_CENT,
    buy_fees,
    walk_buy,
    whole_cent_worst_case,
)


def _timestamp(value: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError("capture timestamp must include a timezone")
    if value.endswith("Z"):
        value = value.removesuffix("Z") + "+00:00"
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None:
        raise ValueError("capture timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def _json_object(data: bytes, path: Path, *, parse_float=None) -> dict:
    value = json.loads(data, parse_float=parse_float)
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return value


def _recorded_json(root: Path, manifest: dict, path: Path) -> dict:
    relative = path.relative_to(root).as_posix()
    if not path.resolve().is_relative_to(root):
        raise ValueError(f"{relative} must resolve inside the data directory")
    entry = manifest.get(relative)
    if not isinstance(entry, dict):
        raise ValueError(f"{relative} is missing from the capture manifest")
    data = path.read_bytes()
    if len(data) != entry.get("bytes") or hashlib.sha256(data).hexdigest() != entry.get("sha256"):
        raise ValueError(f"{relative} does not match the capture manifest")
    return _json_object(data, path, parse_float=Decimal)


def _find_ticker(rows: object, ticker: str) -> dict | None:
    if not isinstance(rows, list):
        return None
    matches = [row for row in rows if isinstance(row, dict) and row.get("ticker") == ticker]
    return matches[0] if len(matches) == 1 else None


def replay_capture(
    data_dir: Path,
    run_path: Path,
    *,
    city: str,
    ticker: str,
    side: str,
    limit: Decimal,
) -> dict:
    """Estimate a one-contract buy against the city's first eligible book."""
    if side not in {"yes", "no"}:
        raise ValueError("side must be yes or no")
    if not limit.is_finite() or not 0 <= limit <= ONE:
        raise ValueError("limit must be a dollar price from 0 to 1")
    if not city.isascii() or not city.isalpha():
        raise ValueError("city must be a recorder city code")

    root = data_dir.resolve()
    run_file = (root / run_path).resolve()
    if not run_file.is_relative_to(root):
        raise ValueError("run manifest must be inside the data directory")
    run = _json_object(run_file.read_bytes(), run_file)
    result = {
        "run_id": run.get("run_id"),
        "city": city,
        "ticker": ticker,
        "side": side,
        "limit_price": limit,
        "requested_quantity": ONE,
        "fee_schedule_effective_date": FEE_SCHEDULE_EFFECTIVE_DATE,
    }

    cutoffs = [cutoff for cutoff in run["cutoffs"] if city in cutoff["cities"]]
    if not cutoffs:
        return {**result, "status": "skipped", "reason_code": "city_not_captured"}
    if len(cutoffs) != 1:
        raise ValueError("run contains more than one cutoff for this city")
    cutoff = cutoffs[0]
    capture = cutoff["cities"][city]
    result.update(
        event_ticker=capture["event_ticker"],
        target_date=capture["target_date"],
        cutoff_utc=cutoff["cutoff_utc"],
        capture_reason_code=capture.get("reason_code"),
    )
    if capture.get("eligible") is not True:
        return {**result, "status": "skipped", "reason_code": "capture_ineligible"}
    if ticker not in capture["tickers"]:
        return {**result, "status": "skipped", "reason_code": "ticker_not_discovered"}

    books = cutoff["books"]
    received = _timestamp(books["first_received_at"])
    cutoff_time = _timestamp(cutoff["cutoff_utc"])
    capture_dir = run_file.parent / f"cutoff_{cutoff_time:%Y%m%dT%H%MZ}"
    manifest = run["files"]
    markets_path = capture_dir / city / "kalshi_markets.json"
    markets = _recorded_json(root, manifest, markets_path)
    market = _find_ticker(markets.get("markets"), ticker)
    if market is None or market.get("event_ticker") != capture["event_ticker"]:
        return {**result, "status": "skipped", "reason_code": "market_identity_unverified"}
    if market.get("status") != "active" or not (
        _timestamp(market["open_time"]) <= received < _timestamp(market["close_time"])
    ):
        return {**result, "status": "skipped", "reason_code": "market_not_active"}

    book_path = capture_dir / "books_1.json"
    book_source = book_path.relative_to(root).as_posix()
    batch = _recorded_json(root, manifest, book_path)
    result.update(
        first_book_received_at=books["first_received_at"],
        receipt_lateness_seconds=books["receipt_lateness_seconds"],
        recheck_gap_seconds=books.get("recheck_gap_seconds"),
        first_book_path=book_source,
        first_book_sha256=manifest[book_source]["sha256"],
    )
    book = _find_ticker(batch.get("orderbooks"), ticker)
    if book is None or not isinstance(book.get("orderbook_fp"), dict):
        return {**result, "status": "skipped", "reason_code": "first_book_unavailable"}
    try:
        walk = walk_buy(book["orderbook_fp"], side=side, limit=limit)
    except ValueError as exc:
        return {**result, "status": "skipped", "reason_code": "book_unparseable", "detail": str(exc)}
    result.update(best_ask=walk.best_ask, quantity_at_limit=walk.quantity_at_limit)
    if not walk.fills:
        return {**result, "status": "skipped", "reason_code": "insufficient_depth"}

    series_path = capture_dir / city / "kalshi_series.json"
    series_source = series_path.relative_to(root).as_posix()
    try:
        series = _recorded_json(root, manifest, series_path).get("series")
    except (OSError, ValueError) as exc:
        return {**result, "status": "skipped", "reason_code": "fee_metadata_unavailable", "detail": str(exc)}
    if not isinstance(series, dict) or series.get("ticker") != capture["series"]:
        return {**result, "status": "skipped", "reason_code": "fee_series_unverified"}
    result.update(
        fee_series_path=series_source,
        fee_series_sha256=manifest[series_source]["sha256"],
        fee_type=series.get("fee_type"),
    )
    if result["fee_type"] != "quadratic":
        return {**result, "status": "skipped", "reason_code": "fee_type_unsupported"}
    captured_multiplier = series.get("fee_multiplier")
    if type(captured_multiplier) not in (int, Decimal):
        return {**result, "status": "skipped", "reason_code": "fee_multiplier_invalid"}
    multiplier = Decimal(captured_multiplier)
    if not multiplier.is_finite() or multiplier < 0:
        return {**result, "status": "skipped", "reason_code": "fee_multiplier_invalid"}

    worst = whole_cent_worst_case(walk.fills, multiplier=multiplier)
    return {
        **result,
        "status": "simulated_fill",
        "fills": [asdict(fill) for fill in walk.fills],
        "notional": walk.notional,
        "fee_multiplier": multiplier,
        "primary_fee_scenario": None,
        "whole_cent_best_case": asdict(buy_fees(
            walk.fills, multiplier=multiplier, balance_precision=WHOLE_CENT)),
        "sub_cent_best_case": asdict(buy_fees(
            walk.fills, multiplier=multiplier, balance_precision=SUB_CENT)),
        "whole_cent_worst_case": {
            "balance_precision": worst.balance_precision,
            "fill_quantity": MIN_FILL_QUANTITY,
            "fill_count": len(worst.per_fill),
            "net_fee": worst.net_fee,
            "total_debit": worst.total_debit,
        },
    }
