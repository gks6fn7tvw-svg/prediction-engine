# First-snapshot paper replay

Replay a caller-selected, one-contract buy from a local copy of the recorder's data repository.

```sh
python -m paper \
  --data-dir /path/to/prediction-engine-data \
  --run runs/YYYY/MM/DD/RUN_ID/run.json \
  --city NY --ticker MARKET_TICKER --side yes --limit 0.36
```

The replay reads the quadratic fee type and multiplier from the city's saved series response. The output records that file's hash and the fee schedule date. Missing, invalid, or unsupported fee metadata produces a skip.

The replay requires the city's own `eligible` flag and checks the market identity and active window. It verifies the raw discovery, series, and first-book files against the run manifest. The JSON report includes the displayed quantity at the limit and each consumed price and size. Insufficient depth or a malformed book level produces a skip.

The buy walks the asks implied by the opposite-side bids, from the lowest price through the original limit. Each fee scenario applies the general taker formula, microdollar rounding, balance alignment, and the capped per-order rebate. [Kalshi order books](https://docs.kalshi.com/getting_started/orderbook_responses) · [Fee schedule](https://kalshi.com/docs/kalshi-fee-schedule.pdf) · [Fee rounding](https://docs.kalshi.com/getting_started/fee_rounding)

## Fee scenarios

- `whole_cent_best_case` groups each consumed price level into one fill.
- `whole_cent_worst_case` uses 0.01-contract fills along the same price ladder and reports their combined debit. It is the primary scenario while account precision is unconfirmed. [Kalshi quantity precision](https://docs.kalshi.com/getting_started/fixed_point_migration#fractional-contracts)
- `secondary_sub_cent` groups each consumed price level into one fill and aligns the balance to $0.0001.

With the rebate cap, the cumulative debit after each fill is the greater of two amounts. One is the cumulative notional plus rounded trade fees, rounded up together to the balance grid. The other is the previous debit plus this fill's notional rounded up to that grid. Splitting a price level into minimum-size fills can only increase the cumulative rounded trade fees and the sum of rounded notionals, so it maximizes the debit for that price ladder.

For one 5¢ contract at a multiplier of 1, the whole-cent debit is 6¢ as one fill or $1.00 as 100 minimum-size fills. The sub-cent price-level estimate is 5.34¢.

This is a first-snapshot estimate for one candidate. The v0.3 trading experiment also requires an entry policy, a recorded recheck and settlement accounting.

```sh
python -m unittest tests.test_paper
```
