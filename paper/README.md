# First-snapshot paper replay

Replay a caller-selected, one-contract buy from a local copy of the recorder's data repository.

```sh
python -m paper \
  --data-dir /path/to/prediction-engine-data \
  --run runs/YYYY/MM/DD/RUN_ID/run.json \
  --city NY --ticker MARKET_TICKER --side yes --limit 0.36 --fee-multiplier 1
```

Use the effective taker multiplier for the selected market and date. The output records the multiplier and the fee schedule date.

The replay requires the city's own `eligible` flag and checks the market identity and active window. It verifies the raw discovery and first-book files against the run manifest. The JSON report includes the displayed quantity at the limit, each simulated fill, and its fees under whole-cent balance alignment (primary) and $0.0001 alignment (secondary). Insufficient depth produces a skip and preserves the displayed quantity in the report.

The buy walks the asks implied by the opposite-side bids, from the lowest price through the original limit. Each consumed price level is modeled as one fill. The fee calculation applies the general taker formula, microdollar rounding, balance alignment and the capped per-order rebate. [Kalshi order books](https://docs.kalshi.com/getting_started/orderbook_responses) · [Fee schedule](https://kalshi.com/docs/kalshi-fee-schedule.pdf) · [Fee rounding](https://docs.kalshi.com/getting_started/fee_rounding)

This is a first-snapshot estimate for one candidate. The v0.3 trading experiment also requires an entry policy, a recorded recheck and settlement accounting.

```sh
python -m unittest tests.test_paper
```
