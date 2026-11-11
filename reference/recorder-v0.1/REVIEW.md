# Recorder reference and review

November 10, 2026, Pacific/Honolulu. This is a new source/reference capture, not an independently repeated audit of First_Capture_v0_1. No service or recurring collection was deployed.

## Fee model

All five newly captured series responses report fee_type=quadratic and fee_multiplier=1. For immediately executed quantities, the current standard unrounded taker model in dollars is 0.07 * multiplier * quantity * price * (1-price). Apply it at the actual execution levels, not to the average price of a multi-level fill. Capture effective event/market exceptions and fee changes as well as series metadata before applying the formula.

Important correction: the currently retrieved July 7, 2026 schedule specifies sub-cent cash alignment. The current API rounding documentation specifies trade fees rounded upward to six decimal dollars, balance alignment at $0.0001 for direct members or $0.01 for non-direct members, and a per-order rounding accumulator/rebate across fills. Therefore always rounding each fee up to one cent is only a conservative scenario, not a universally exact fee calculation. The PDF's illustrative fee table still displays whole-cent values; retain that discrepancy and use the current documented execution/account rounding mechanics. Member/account precision has not been verified for Seth. No account credentials were used in this research.

For one fill of one contract at $0.35, multiplier 1, the model fee is $0.015925. The documented direct-member buy alignment gives total cash cost $0.3660, fee $0.0160; a non-direct member cent-aligned scenario gives cost $0.37, fee $0.02. Multiple fills require the accumulator logic rather than independent cent rounding. No settlement fee is listed in the current standard schedule.

Sources: https://kalshi.com/docs/kalshi-fee-schedule.pdf and https://docs.kalshi.com/getting_started/fee_rounding . Raw bytes and retrieval metadata are included. No claim is made that the fee schedule remains constant for future events.

## Required design changes

1. Freeze the scheduled UTC cutoff from the city's IANA time zone, and explicitly register allowed lateness, e.g. a proposed 120 seconds subject to measured startup delay. An actual local hour of 18 is not sufficient to accept any start during that hour. Record scheduled, actual-start, request, receipt and monotonic elapsed times. Skip/flag late runs; do not relabel them as on-time.
2. Separate capture scheduling in local civil time from the climate measurement window. NWS CLI daily periods are midnight-to-midnight local STANDARD time; during daylight time that is 1 a.m. to 1 a.m. local civil time. Verify the current contract/provider's exact window, and align forecast hours accordingly. Do not assume midnight-to-midnight IANA civil time is the correct label period. Source: https://www.weather.gov/lot/weather_observations_faq .
3. Freeze forecast information available by the cutoff. Fetch/cache forecasts before it where feasible. If a late fetch returns an issuance newer than the cutoff, skip or mark it exploratory; do not backdate it. Save forecast generation/update/valid times and full hourly/grid responses. An hourly temperature maximum is a predictor, not an exact daily climate high; train its residual error accordingly.
4. Use markets responses for discovery/rules and the first batch book response for quote selection and primary midpoints. Keep separate times. One batch HTTP call does not itself establish exchange-wide atomicity. Discover markets and collect metadata early enough to reduce delay at the cutoff.
5. Recheck after at least one measured second from the first book's receipt using a monotonic clock; save all books again. Keep the selected side and original limit fixed. Store actual delay; do not assume an HTTP request or a sleep proves exactly one second of exchange delay.
6. Walk fractional depth to the requested whole quantity. If 0.01 contracts cost $0.35 and 0.99 cost $0.36, the purchase price is $0.3599 before fees; $0.36 is the maximum required level. Record each consumed quantity/price and sum per-level fee models. Skip a full-contract fill if total qualifying depth below the original limit is insufficient. Observed depth still does not prove execution.
7. Keep the registered quoted-midpoint benchmark, but separately report one-contract executable bid/ask cost. Tiny best-price quantities can make the displayed midpoint unrepresentative of the planned trade. Tight spreads do not establish probability accuracy or predict how often a 10-cent model edge occurs.
8. Do not use one next-day fetch as a final settlement guarantee. Poll pending events to finalized payouts, preserve initial and revised responses, and retain scalar/exception settlements. Make lookback/retry windows explicit; handle historical endpoint handoff for older unresolved records.
9. Save raw weather portal HTML plus the actual displayed data source or rendered DOM where feasible. A JavaScript HTML shell alone is not the table's raw evidence. Preserve capture time, selected date and report revision. Keep NWS CLI/CF6 raw product versions as additional evidence; never replace provider evidence with a transcription. No new weather.com raw page was captured in this API-focused reference run.
10. Use append-only capture keys by source/city/target-date/cutoff/run-id, with immutable manifests. Retry uploads idempotently, verify remote persistence, and preserve failures. Hashes show byte integrity, not independent proof that a response came from the claimed live endpoint.
11. If GitHub is the sink, use a separate data repository/branch or build filters so data commits do not repeatedly rebuild the Render job. Use narrowly scoped repository credentials as server secrets; never store Kalshi keys, GitHub tokens, account balances or personal data in captured public-data files. A private repo does not automatically grant Claude access: Seth must connect an authorized integration.

## Render

Render cron is a reasonable proposed runtime for bounded scheduled collection. Official docs specify UTC schedules, no persistent disk, a $1 monthly minimum per cron service, delayed scheduled runs when a prior run overlaps, and cancellation of an active run when manually triggered. The delay/cancellation behavior reinforces lateness and partial-upload handling. Avoid overnight retry loops that block the next cutoff.

Source: https://render.com/docs/cronjobs . Paid deployment awaits Seth's explicit confirmation/budget as the relayed message states. No billable resource was created and the previous $0 constraint was not silently discarded.

## Scope of this capture

The package includes fresh raw series objects, official NWS station metadata, point-to-grid mappings, hourly forecasts, current CLI product pages, and fee references, each with request/receipt times and hashes. Intermediate CLI reports may only summarize the day through 4 p.m.; these are station-mapping evidence, not final settled labels. Current forecast files are reference snapshots, not forecasts frozen at the experimental cutoff. No extra NBM history audit was performed; the earlier single-date listing remains existence evidence only.
