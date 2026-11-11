# Prediction Engine: design draft and decision record

Version 0.3 | November 10, 2026 (Pacific/Honolulu) | For Seth and Claude's review

Status: proposed research design. Version 0.3 specifies recorder inputs, cutoff eligibility, settlement labels and fee scenarios. The saved snapshots are engineering audit material. Recorder implementation and approval of the Render budget are pending.

## One-page working specification

**Objective.** Determine whether one frozen numerical weather model improves probability forecasts over contemporaneous Kalshi prices. Assess trading economics separately. This specification assumes no reusable Prediction Engine code or validated research dataset.

**1. Category and cities.** Daily maximum temperature in New York, Chicago, Miami, Denver and Los Angeles. Schedule each capture at 6 p.m. local civil time on the preceding day. Match forecast hours to the contract's climate day using the verified station and measurement window. All ranges for one city and date form one event. Preserve cross-city and nearby-day dependence in evaluation.

**2. Data and labels.** Record raw NWS gridpoint and hourly forecasts with their issue, update and receipt times. Save Kalshi discovery and rules, two timestamped batch order books, fee metadata, and finalized market results and dollar payouts. Record the exchange expiration value separately as a candidate measured temperature. Use NWS CLI reports for reconciliation. Weather Company portal capture is optional audit evidence. Historical inputs and labels require the compatibility audit below.

**3. Benchmark and models.** Use Yes bid/ask midpoints from the first eligible batch order book. The NWS control uses the maximum hourly temperature over the complete climate day, then fits station-specific errors on earlier matched outcomes. Preserve daytime MaxT and its valid interval for comparison. Evaluate one numerical NBM candidate during development and register one primary model before the future test. The weather probability model uses numerical inputs.

**4. Primary test, calibration and economics.** Primary result: paired Brier-score improvement over the market, scoring every eligible event, including trade skips, with equal city weights. Use a two-sided 95% confidence interval for the improvement; a lower bound above zero establishes forecast skill for the tested setting. Plan sample size for 80% power at a true 5% relative Brier improvement; 5% is a planning effect, not an extra observed-score pass gate. Calibration is diagnostic initially: publish fixed probability groups with dependence-aware intervals and effective support, without v0.1's 50-observation precision claim. Paper profit, drawdown and fill rate are separate secondary results; forecast skill alone does not establish a tradable advantage.

**5. Timeline and paper convention.** Start a 30-calendar-day engineering pilot once the recorder operates. Begin with a 10-minute lateness limit on first-book receipt and require the control forecast to be available by the scheduled cutoff with an earlier NWS update time. Preserve and flag late records. Use development data to register the confirmatory duration, initially budgeting at least 120 future calendar days and at least 95% usable paired records overall and per city. Simulate one whole contract per city/event with fractional depth, the provisional 10-cent entry buffer, the one-second recheck and the five-second stress test. Use cent-aligned balance accounting as the conservative primary scenario and sub-cent alignment as sensitivity until account precision is verified.

## Recorder requirements for v0.3

### Scheduling and eligibility

Run one bounded hourly job. Determine due cities from the scheduled UTC hour converted to each city's IANA time zone. Use the scheduled local date to choose the following day's event. Record the schedule, actual process start, source request and receipt times, and monotonic elapsed durations. The initial book must be received from the scheduled cutoff through 10 minutes after it, inclusive. Apply this limit to receipt so a slow request cannot qualify by starting on time. Preserve early and late records with timestamps and a reason code. They remain visible in the scheduled-event coverage denominator and are excluded from confirmatory paired scores.

For the control, require NWS updateTime before the cutoff and evidence the inputs were available by the cutoff. The hourly job can cache forecast snapshots on an earlier invocation. If the fetch after the cutoff returns a new run, retain it for audit and use an eligible cached forecast only under the registered freshness and version policy. An update timestamp alone does not establish public availability. Freeze that selection and staleness policy before the confirmatory window. Save raw forecast versions separately when gridpoint and hourly products have different update times.

Climate-day slicing follows the verified settlement window, typically midnight to midnight local standard time for these CLI products. During daylight saving time this spans 1 a.m. to 1 a.m. on the local civil clock. Store the target-day boundaries in UTC and use the half-open interval from start through just before end. Verify 24 hours of coverage, units, and ordered timestamps. Flag missing or duplicated samples. Retain the raw gridpoint temperature and maxTemperature series, including unit and validTime intervals. Compute the control's full-day maximum from the eligible hourly temperatures. Fit measured station-level forecast errors. A sampled peak can miss a between-hour maximum and the forecast itself can err in either direction.

### Books, fees and persistence

Use market discovery responses for rule and event identity checks. Take both bid and ask inputs from the same initial batch-book response and record book receipt separately from discovery. Start the recheck request at least one measured second after initial book receipt and save its actual timing. Preserve the original side and price limit. Walk the fractional quantities to a full contract and retain each simulated price and quantity. Record insufficient depth as a skip.

The conservative primary fee scenario uses a one-cent balance grid. The secondary scenario uses a $0.0001 balance grid. Apply 0.07 times the effective multiplier times quantity times price times (1 minus price) to each fill level, then follow Kalshi's six-decimal fee rounding and per-order rounding accumulator and rebate mechanics. Include the applicable account precision and schedule version in results. For one fill of one contract at 35 cents, the total costs are $0.3700 and $0.3660 respectively. At 2 cents, the corresponding net fees are 1 cent and 0.14 cents. Confirm the actual account precision before applying a fee scenario to live execution.

Use a separate private repository for recorded data and a separate code repository for Render builds. Key captures by series, event date, scheduled cutoff and run ID. Store raw bytes with a SHA-256 manifest and retrieval times. Verify successful remote persistence and make retries idempotent. Keep credentials in deployment secrets.

### Final labels and polling

Poll unresolved events on the hourly job after the time-sensitive capture work. Set a finite time and request budget and resume pending outcomes on later runs. Store each settlement response and its timestamp, including revisions. Use finalized Kalshi result and settlement_value_dollars for binary scoring and payout accounting. A final Yes result supplies a binary label of 1 and a final No result supplies 0. For a $1 contract, the No payout is $1 minus the final Yes settlement value. Record scalar or exceptional payouts in paper profit and flag them outside binary forecast scoring.

Record expiration_value as a separate field. Use it for temperature residuals when units, rule identity and the finalized outcome are consistent. Preserve the exact NWS CLI issue for cross-checking. A missing numeric label leaves temperature residual training pending for that event, while the finalized payout can still score the market outcome. Weather Company portal capture is best effort. Save the source response or rendered report with date and revision when access permits. Portal failures do not invalidate a run.

The Render proposal uses an hourly cron job and separate code and data repositories. Render documents a $1 monthly minimum per cron service. Seth's approval of the paid deployment remains pending. The public Kalshi and NWS reference inputs support local implementation and parser testing before deployment.

## Forecast and settlement audit for v0.3

All 61 reference-package manifest entries and all 20 first-capture entries passed their hash checks. The reference archive contains hourly forecasts and point-to-grid mappings. A historical raw gridpoint temperature payload for those same requests was not captured.

The saved November 11 hourly forecasts cover 24 target hours for every city. All five maxima fall in daytime hours, so limiting these particular hourly samples to 7 a.m.–7 p.m. would give the same peaks. NWS defines its daytime MaxT separately from the temperature forecast valid at each hour. A fresh Miami grid response reported a daytime MaxT of 85°F for November 11, with updateTime 2026-11-10T20:36:50Z. The saved Miami hourly peak was 84°F. Keep both raw values and their provenance, and fit station errors using resolved training observations.

| City | Saved hourly peak | Highest two-sided Yes midpoint | First book relative to proposed cutoff |
| --- | ---: | --- | ---: |
| New York | 61°F | 61–62°F at 62.5% | +2 h 10 min 38 s |
| Chicago | 55°F | 55–56°F at 45.5% | +1 h 10 min 38 s |
| Miami | 84°F | 85–86°F at 72.5% | +2 h 10 min 38 s |
| Denver | 63°F | 65–66°F at 58.5% | +10 min 38 s |
| Los Angeles | 86°F | 82°F or below at 60.5% | −49 min 22 s |

The forecast responses arrived about 37 min 32 s after the book responses. The Los Angeles 85–86°F bracket had a 3.5% midpoint in the earlier snapshot. Five of the 30 contracts lacked a two-sided midpoint in the stored books. The current cutoff rules exclude all five event comparisons from confirmatory testing. New York and Chicago also had NWS update times after their proposed cutoff.

A live public API check of finalized November 9 winners across the five cities returned expiration values of 58°F, 61°F, 85°F, 72°F and 86°F, respectively, with settlement_value_dollars = 1.0000 on each winning Yes contract. Those expiration values agree with the previously observed Weather Company daily maxima for that date. The fields support separate temperature and payout labels.

## Calibration correction and recalculation

Claude correctly identified that v0.1's sample-size floor was inconsistent with the precision it demanded. Fifty dates were only a minimum support screen, but the draft then implied that a narrow simultaneous calibration interval could reasonably be established on that schedule. That was not justified.

For an illustrative group of independent Bernoulli outcomes with probability near 50%, a 95% Wilson interval at 50 observations has a half-width of about **13.4 percentage points**, not 5. A standard normal sample-size approximation for half-width h is:

    n approximately z^2 * p * (1-p) / h^2

With p = 0.5 and h = 0.05:

| Illustrative requirement | Critical value | Independent observations per group |
| --- | ---: | ---: |
| One 95% interval with approximately +/-5-point precision | 1.960 | 385 |
| Five simultaneous intervals with the same precision, using Bonferroni correction | 2.576 | 664 |

These rounded normal-approximation counts are planning illustrations, not guarantees for weighted forecast groups. The Wilson calculation and the normal approximation are different methods; the first row is conventionally rounded to roughly 400. Within-event relationships, cross-city weather, serial dependence, varying predictions and uneven group membership change the actual uncertainty.

There is a second distinction: **precision is not power to pass an equivalence gate**. An interval whose half-width is just 5 points will fit entirely inside a +/-5-point tolerance only when the estimated calibration gap is extremely close to zero. Under the same idealized normal assumptions, achieving an 80% chance of an entire two-sided 95% interval lying inside +/-5 points when the true gap is zero requires approximately:

    n approximately (z + 1.2816)^2 * 0.25 / 0.05^2

This gives **1,051 independent observations for one group**, or **1,488 for a single group using the five-group Bonferroni critical value**. The latter is not an 80% joint-power calculation for all five groups. Dependence and nonzero true gaps can raise the requirement substantially.

### Revised calibration policy

- Initially treat calibration as an estimation task, with no claim that failure to detect miscalibration proves calibration.
- Keep the five groups fixed: [0, .2), [.2, .4), [.4, .6), [.6, .8), and [.8, 1]. Use day/event weights consistently and report distinct dates, city-events, and dependence-adjusted uncertainty. Do not present a single effective sample size as exact.
- Keep Yes/No interpretations explicit: model No probability is 1 minus Yes probability. Do not count both as independent evidence. Report calibration of the actual selected side separately, retaining its original city/date cluster.
- Show pooled and city-specific diagnostics. A favorable pooled plot cannot establish performance in every city, season, regime or selected-trade subgroup.
- Before any claim that a traded probability band is calibrated to a specified tolerance, define the tolerance, simultaneous-error control, power target and required support using development data. Freeze these before a separate confirmatory test. +/-5 points remains a possible later target, not an achievable promise for the first pilot.
- A primary forecast-score pass may coexist with insufficient calibration evidence or negative trading economics. Report those statuses separately.

## Multi-city measurement and power plan

### Initial city set

The following series were checked through unauthenticated Kalshi series and open-market endpoints during this revision:

| City | Series / CLI product | NWS station | NWS grid | Forecast scheduling zone |
| --- | --- | --- | --- | --- |
| New York City | KXHIGHNY / CLINYC | KNYC Central Park | OKX/34,45 | America/New_York |
| Chicago | KXHIGHCHI / CLIMDW | KMDW Midway | LOT/72,69 | America/Chicago |
| Miami | KXHIGHMIA / CLIMIA | KMIA Miami International | MFL/105,51 | America/New_York |
| Denver | KXHIGHDEN / CLIDEN | KDEN Denver International | BOU/75,66 | America/Denver |
| Los Angeles | KXHIGHLAX / CLILAX | KLAX Los Angeles International | LOX/149,41 | America/Los_Angeles |

The Recorder Reference archive contains the official station coordinates, point lookups and CLI reports for these mappings. Validate the station and rule version on every event and refresh the grid mapping periodically. Keep the five-city cohort fixed through the evaluation.

### Event and aggregate scores

For each ordinary binary settlement, calculate (predicted Yes probability minus outcome)^2 for every supported temperature range. Average within the city/date, then average paired differences within each city and give each of the five cities weight 1/5 in the pooled result. Compare the model and market on exactly the same contracts at the same cutoff.

The market benchmark is the Yes bid/ask midpoint, not the last-trade price, the price we can buy at, or guaranteed true probability. Preserve and report any lack of coherence across market midpoints rather than silently normalizing it. Missing two-sided quotes cannot be repaired with later prices. Score legitimate trade skips. Missing score inputs remain missing and count against coverage; do not turn them into zero error or a simulated successful skip.

Calculate relative Brier skill as 1 minus model mean score / market mean score, with the same city weighting for both. If the denominator is zero, the ratio is undefined. The primary hypothesis concerns positive paired score improvement; 5% relative improvement specifies the useful effect for power planning, rather than imposing an additional point-estimate threshold that would alter the planned power.

### Dependence and sample size

Use complete target-date vectors containing all cities and their missingness indicators. Resample consecutive date blocks together, preserving cross-city relationships and neighboring-day dependence. Seven-day blocks are a proposed starting point; choose the primary interval/power procedure from development data and report sensitivity to longer blocks. A four-month test does not validate every season, and bootstrap intervals are approximate.

The 30-day pilot estimates collection reliability and liquidity and gives an initial variance/correlation estimate. It is not automatically enough to fit stable city-specific error models or estimate long-run variance. Extend development or use audited archives where necessary. Archive-assisted development must retain original run availability and cannot include future test outcomes.

Before a confirmatory test, estimate its duration for 80% power at a true 5% relative Brier improvement using paired, dependence-preserving development resampling. Freeze one primary model, city set, forecast cutoff, sample window, missing-data rules and significance procedure. Budget at least 120 future calendar days, but only commit to an endpoint after the power calculation. If adequate power cannot be estimated credibly or requires an impractical duration, state that limitation rather than relabeling a short pilot as confirmation.

Do not divide the number of required dates by five without estimating dependence. Six hundred raw city-days are not six hundred independent samples. Extra dates are not appended opportunistically until an interval turns positive. Any later evaluation needs a fresh registration and must account for earlier evidence if combined with it.

## Data-source audit

### NOAA National Blend of Models

The NOAA-managed public-data registry documents NBM forecast guidance, public S3 buckets, hourly updates, and access without an AWS account. During this revision, an unauthenticated S3 listing returned dated GRIB2 objects for August 14, 2026, including a CONUS-prefix check in the evidence capture. This verifies that dated archive objects exist; it does not verify every required run, variable, station, horizon or day.

NBM is model guidance, not identical to an archived human-edited NWS point forecast. It can support a numerical candidate; it cannot silently be substituted into an NWS-only control's historical track record. Read model cycles, forecast lead times and original publication/availability information. Initialization time alone does not establish that a run was public before our decision cutoff. Reconstructing a full point-in-time dataset, parsing/subsetting the files and checking gaps remain work to do. Public data access does not guarantee zero compute or storage cost.

### Open-Meteo archives

The official documentation distinguishes four products:

| Product | Relevance to this experiment |
| --- | --- |
| Historical Weather | Reanalysis/analysis; do not use it as a historical forecast or as the contract's authoritative settlement label. |
| Historical Forecast | Stitches early hours of successive runs; does not reproduce one forecast available at our prior-evening cutoff. |
| Previous Runs | Fixed lead offsets for each valid hour; useful for general lead-time studies, but the resulting daily sequence can mix runs issued at different times. |
| Single Runs | Preserves an individual initialization and full horizon; the most appropriate candidate for a fixed-decision-time reconstruction. Still audit publication lag and revisions. |

Current Single Runs documentation lists ECMWF IFS HRES coverage from March 2024 and other models from April 2, 2026. Exact required model/location/variable coverage has not been downloaded or verified here. The pricing page allows free-tier evaluation/prototyping but reserves commercial use for paid access. Do not promise free production trading access. Direct NOAA data is the initial candidate for the $0 constraint.

An important inferred leakage risk: computing a next-day maximum from a rolling fixed-24-hour-lead sequence can include late-day forecasts issued after the preceding 6 p.m. cutoff. Require a run available before the cutoff for the entire target day; do not select a better run in hindsight.

### The Weather Company reports and the source change

Gizmodo reports an August 14 NYC source change; per-city contract cutovers remain unaudited. Kalshi's primary partnership announcement is dated August 27, 2026. An announcement date and a contract's effective rule date are different facts. Neither establishes that all five cities changed simultaneously or that every needed outcome is accessible.

The live weather.com/kalshi portal was inspected directly because search extraction did not expose its data table. Its Daily Climate view showed Max (F) readings and Final statuses for the five cities. November 9 and November 6, 2026 daily reports were checked; the inspected date menu offered November 6-9. This confirms some historical daily-high access, not a multi-month archive or public automation API.

The portal states that its daily values come from NWS/NOAA official Daily Climate Reports, with CF6 backup, and describes revisions when later reports change the applicable determination. Thus, a change in named verifier does not by itself establish a change in the underlying measured quantity. However, equal raw observations do not prove identical rounding, report timing, first-versus-revised values or exchange treatment.

Preserve provider/rule versions. Audit an overlapping sample against the exact rules and settled payouts before pooling older NWS labels. Record mismatch rates and uncertainty by city and revision status. Older data can remain useful for physical forecast-error research even when it is unsuitable as a historical payout label. A source-difference model requires genuinely paired comparable observations; do not invent a provider adjustment from the mere fact of a partnership.

Final market payouts remain authoritative for paper profit. Extraordinary scalar/fair-price settlements remain in profit accounting and are separately flagged; do not pretend they are binary weather outcomes for Brier scoring. Unresolved outcomes remain unresolved. A portal Final label does not override the exchange's contractual revision provisions.

### Order books and forward collection

Kalshi's public API exposes executable-side bids/asks through complementary Yes/No bids and available quantities. Historical trades and candles do not establish historic executable depth. A later recorder should capture full discovery/rule responses, synchronized or explicitly timed books, and a one-second recheck, along with run availability and forecast input bytes. Record receipt times and latency rather than assuming snapshots from different cities were atomic.

A one-off public-data audit capture was made during this revision for the November 11, 2026 daily-high events across the five cities. It contains market discovery responses, order-book snapshots, collection timestamps, an NBM archive listing, and a source-audit note. It has no fitted probabilities, NWS forecast input capture, later outcomes, real orders, or proof of fills. It was not taken at each city's scheduled 6 p.m. cutoff and is excluded from confirmatory evidence.

The first implementation milestone is a bounded hourly recorder with durable storage. The proposed Render deployment depends on Seth's budget approval.

## Paper accounting and result interpretation

Keep the one-second recheck as a declared convention; measure actual runtime latency before applying it to a deployment claim. Simulate at most one whole contract per city/event at the original price limit or better, only with sufficient displayed quantity and an active contract. Save the quoted quantity, partial/unfilled cases, book age and recheck time. Snapshots cannot establish real execution or queue priority.

The provisional entry threshold remains 10 cents per $1 payout after the primary cent-aligned fee scenario and a prior-data-based uncertainty allowance. Review it on development data before registration. Buying at the ask includes the spread. Keep the secondary sub-cent results alongside the primary scenario. Include applicable operating costs in any production profitability claim. Report aggregate correlated exposure across all five cities and worst cumulative decline.

Remove v0.1's 100-fill requirement as a gate for a forecast-skill finding. The number of trades needed for an economic finding depends on dispersion, dependence, costs, fill rates and the effect we aim to detect. Sparse trades can leave economics inconclusive even when the primary forecast test succeeds. Profitability, calibration equivalence, opening-speed advantage and live feasibility are separate hypotheses that need their own powered plans.

At the registered primary endpoint:

- **Forecast skill supported:** the pooled paired-improvement 95% interval has a lower bound above zero and integrity/coverage requirements are met. State the estimated effect, interval, period and city breakdown. This establishes neither universal calibration nor net profit.
- **Practical forecast target ruled out:** the confidence interval excludes the prespecified useful effect on the unfavorable side. If the interval still supports a small positive improvement, report both facts; a model can have detectable but practically insufficient skill.
- **Inconclusive:** the interval covers materially different conclusions, or the planned evidence is insufficient. Do not call a statistically nonsignificant result proof that no edge exists.
- **Invalid:** leakage, incompatible labels, corrupt books, or critical rule-mapping defects undermine the experiment. Fix and register an appropriate independent evaluation.

Secondary diagnostics must not be promoted into whichever primary result looks best. Choose one primary candidate on development data; if several models are claimed confirmatorily, register multiplicity handling. Negative paper results remain visible even if forecast scoring improves.

## Preserved engineering and scientific judgments

- Seek measurable pricing advantage; complexity, novelty, speed, a high win rate, and confident explanations are not evidence of it. Likely winners can be overpriced.
- Keep Prediction Engine reusable and separate from execution. Forecast outputs include event/rule identity, probabilities, estimated uncertainty, source references, timestamps, version, and usable/skip status. The bot handles costs, exposure and order reconciliation.
- Use category-specific models and verify every new category independently. Weather is the starting experiment, not an established easier source of profit.
- Keep LLM probability generation and discretionary AI weather adjustments out of this trial. The later hybrid design may use AI for rules and sourced event information where a new test demonstrates value. Repeated AI answers are not independent evidence.
- Prepare reusable research before fast price decisions. Treat opening-speed advantage as a later experiment.
- Missing/stale inputs, ambiguous rules or uncertainty exceeding an apparent edge produce a skip; preserve all skipped and failed cases.
- Keep models and policies stable through each registered test. Preserve unsuccessful experiments, and give challengers fresh evaluation data.
- Group exposures by their actual shared drivers. More cities diversify some weather risk but do not eliminate shared systems, source errors or market-wide effects.
- Respect the $0 additional-spending constraint. Do not assume free historical APIs permit production trading, or that a chat/dashboard stays running unattended. No paid service or real-money action was initiated.
- No trading edge has been established. A scientifically sound negative or inconclusive finding is useful.

## Sources and evidence

Checked November 10, 2026, Pacific/Honolulu. Numerical sample-size calculations are our calculations under the stated assumptions; sources do not prescribe this project's thresholds.

1. [NIST confidence intervals for proportions](https://www.itl.nist.gov/div898/handbook/prc/section2/prc241.htm): Wilson intervals and their assumptions.
2. [NOAA-managed NBM public-data registry](https://registry.opendata.aws/noaa-nbm/): documented public buckets and dataset description. Dated unauthenticated S3-listing evidence is included in the capture.
3. [Open-Meteo Single Runs](https://open-meteo.com/en/docs/single-runs-api), [Previous Runs](https://open-meteo.com/en/docs/previous-runs-api), and [Historical Forecast](https://open-meteo.com/en/docs/historical-forecast-api): different archive constructions and coverage.
4. [Open-Meteo pricing and usage distinction](https://open-meteo.com/en/pricing): evaluation/prototyping and commercial access differ.
5. [Weather Company climate portal](https://weather.com/kalshi): direct browser inspection of the Daily Climate table, dates and source note. Full history/access conditions are unverified.
6. [Kalshi partnership announcement](https://news.kalshi.com/p/kalshi-weather-company-partnership): primary announcement dated August 27, 2026.
7. [Gizmodo report supplied by Claude](https://gizmodo.com/kalshi-teams-up-with-the-weather-co-on-markets-that-are-nearly-impossible-to-insider-trade-2000803871): the reported NYC cutover needs contract-level verification.
8. [Kalshi public market data](https://docs.kalshi.com/getting_started/quick_start_market_data), [order books](https://docs.kalshi.com/api-reference/market/get-market-orderbook), and [historical tiers](https://docs.kalshi.com/getting_started/historical_data).
9. [NWS forecast API](https://www.weather.gov/documentation/services-web-api) and [Kalshi fee guidance](https://help.kalshi.com/en/articles/13823805-fees).
10. [Gneiting and Raftery, proper forecasting scores](https://sites.stat.washington.edu/raftery/Research/PDF/Gneiting2007jasa.pdf), and [Novy-Marx, backtesting multiple signals](https://www.nber.org/papers/w21329).

11. [NWS forecast matrices and daytime maximum definition](https://www.weather.gov/mrx/pfm_explain) and [NWS climate observation windows](https://www.weather.gov/lot/weather_observations_faq).
12. [Kalshi Get Market](https://docs.kalshi.com/api-reference/market/get-market), [fee rounding](https://docs.kalshi.com/getting_started/fee_rounding), and [fee schedule](https://kalshi.com/docs/kalshi-fee-schedule.pdf).
13. [Render cron jobs](https://render.com/docs/cronjobs).

## Remaining review items

1. Confirm contract day definitions, historical source cutovers and label-version compatibility for the five verified station mappings.
2. Establish complete source-matched outcome access and a point-in-time forecast-run dataset. The portal's short visible date window is not a complete archive.
3. Establish repository access and confirm Seth's Render budget before creating a billable cron service. Preserve the distinction between a source audit and an operating recorder.
4. Use development evidence for the actual powered duration, city error-model support and dependence-aware interval method.
5. Freeze one primary model. NWS is a control, and a numerical NBM candidate must earn improvement; public NWS information alone may offer no advantage at the evening cutoff.

## Change record

- v0.1: Saved the original engineering judgments and single-city proposal; no engine implementation or edge established.
- v0.2: Accepted Claude's multi-city direction and no-LLM weather trial. Recalculated calibration precision and equivalence power, removed the unsupported four-gate schedule, made market-relative Brier improvement primary, and separated calibration and economics. Verified public archive availability at the object-listing level, checked all five current series, inspected daily-high portal history, qualified the historical-label incompatibility claim, and saved a one-off public market-data audit capture. Recurring collection and confirmatory sample size remain unresolved.

- v0.3: Adopted cent-aligned primary fee accounting, finalized Kalshi labels, raw grid and hourly forecast capture, full climate-day control inputs and the 10-minute first-book deadline. Added verified station mappings and the timestamp audit. Recorded Render with separate code and data repositories as the proposed deployment.

