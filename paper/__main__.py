"""Command-line entry point for a first-snapshot paper replay."""

import argparse
import json
from decimal import Decimal, DecimalException
from pathlib import Path

from .replay import replay_capture


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True, help="local copy of the data repository")
    parser.add_argument("--run", type=Path, required=True, help="run.json path relative to the data directory")
    parser.add_argument("--city", required=True, help="recorder city code")
    parser.add_argument("--ticker", required=True, help="market ticker within the selected event")
    parser.add_argument("--side", choices=("yes", "no"), required=True)
    parser.add_argument("--limit", required=True, help="maximum buy price in dollars")
    args = parser.parse_args()
    try:
        result = replay_capture(
            args.data_dir,
            args.run,
            city=args.city.upper(),
            ticker=args.ticker,
            side=args.side,
            limit=Decimal(args.limit),
        )
    except (OSError, ValueError, KeyError, TypeError, DecimalException) as exc:
        parser.error(str(exc))
    print(json.dumps(result, default=str, indent=2))


if __name__ == "__main__":
    main()
