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

- `whole_cent_best_case` and `sub_cent_best_case` each use one fill per consumed price level, with $0.01 and $0.0001 balance alignment respectively.
- `whole_cent_worst_case` is an upper bound using 0.01-contract fills along the same price ladder. [Kalshi quantity precision](https://docs.kalshi.com/getting_started/fixed_point_migration#fractional-contracts)

The replay reports the grouped estimates side by side and leaves `primary_fee_scenario` as `null` while account precision is unconfirmed. The v0.3 forecast-skill comparison uses forecast probabilities and market midpoints independently of this fee accounting.

With the rebate cap, the cumulative debit after each fill is the greater of two amounts. One is the cumulative notional plus rounded trade fees, rounded up together to the balance grid. The other is the previous debit plus this fill's notional rounded up to that grid. Splitting a price level into minimum-size fills can only increase the cumulative rounded trade fees and the sum of rounded notionals, so it maximizes the debit for that price ladder.

At a multiplier of 1, the upper bound is $1.00 per contract at every whole-cent price from 1¢ to 99¢. Each 0.01-contract fill costs a cent under whole-cent balance alignment, and its fee cap prevents a rebate. Even a full $1 payout at this bound only breaks even before operating costs. At 5¢, the grouped estimates are 6¢ with whole-cent alignment and 5.34¢ with sub-cent alignment.

This is a first-snapshot estimate for one candidate. The v0.3 trading experiment also requires an entry policy, a recorded recheck and settlement accounting.

```sh
python -m unittest tests.test_paper
```
