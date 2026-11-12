# prediction-engine

Research code for testing whether a weather model can out-forecast Kalshi's
daily high-temperature markets. Nothing here trades, places orders, or uses a
Kalshi account. No forecasting or trading edge has been shown yet.

## What exists so far: the recorder

`recorder/` is a data logger. It only reads public data and saves it, so that
forecasts and prices can be compared later against what actually happened.

It covers five cities: New York (KNYC), Chicago Midway (KMDW), Miami (KMIA),
Denver (KDEN) and Los Angeles (KLAX).

It runs once an hour, at minute 52. On each run it does four things.

1. **Cutoff capture.** If a city's 6 p.m. local time is 8 minutes away, it
   fetches that city's market rules and the National Weather Service forecast
   *before* 6 p.m., waits until exactly 6 p.m., takes a snapshot of the order
   books for tomorrow's temperature ranges, and takes a second snapshot at
   least one second later.
2. **Noon capture (same day).** If a city's noon local *standard* time is
   8 minutes away (1 p.m. on the clock during daylight saving), it fetches the
   markets, then the station's observations from the start of the climate day,
   finishing before noon, waits until exactly noon and takes the same two
   order-book snapshots for *today's* ranges. This feeds the same-day test
   (H1). It saves inputs only, never outcomes.
3. **Settlement.** After a day's weather is over, it checks Kalshi each hour
   until the result is final, saving every change along the way.
4. **Climate reports.** It saves the NWS daily climate report for each city
   whenever the report's text changes.

Everything from one run is saved to the data repository
(`prediction-engine-data`) as a single commit.

### What gets saved

```
runs/2026/11/10/<run id>/run.json            what the run did: times, lateness, problems, file hashes
runs/.../cutoff_20261110T2300Z/books_1.json  order books at the cutoff
runs/.../cutoff_20261110T2300Z/books_2.json  order books at least one second later
runs/.../cutoff_.../NY/kalshi_markets.json   market rules and quotes
runs/.../cutoff_.../NY/kalshi_series.json    series details, including the fee type
runs/.../cutoff_.../NY/nws_hourly.json       hourly forecast
runs/.../cutoff_.../NY/nws_gridpoint.json.gz full forecast grid data (gzipped, it is large)
runs/.../cutoff_.../NY/nws_points.json       station-to-grid lookup
runs/.../noon_20261111T1700Z/books_1.json    order books at noon local standard time
runs/.../noon_20261111T1700Z/books_2.json    order books at least one second later
runs/.../noon_.../NY/kalshi_markets.json     open markets at noon
runs/.../noon_.../NY/nws_observations.json   station observations received before noon
runs/.../noon_.../NY/nws_observations_late.json  only if the above missed noon; audit only
settlement/<event>/<run id>.json             each change in an event's settlement status
cli/<city>/<run id>.html                     each new version of the NWS climate report
state/state.json                             the recorder's own bookkeeping
```

Responses are stored byte-for-byte as received. Values that `run.json` works
out (best bid and ask, the forecast's hourly maximum) are for a quick look
only. The raw files are the record.

### Rules it follows

- **The cutoff is real.** The books are requested at 6 p.m. local time, never
  before. A capture counts only if the first order book *arrives* from the
  cutoff through 10 minutes after it, both ends included (design v0.3). It is
  judged on arrival, so a slow response cannot qualify just because its
  request went out in time. Anything else is still saved, marked
  `eligible: false` with a reason code (`late_first_book`, `early_first_book`,
  `first_book_failed`, `missing_books`, `no_markets`), and excluded from
  scoring. Each city is judged on its own books: if an on-time response
  leaves out any of a city's ranges, that city is `missing_books`. A cutoff more
  than 20 minutes gone is not captured at all and is recorded as `missed`.
- **Noon weather must arrive before noon.** Each station's observations are
  fetched on its own background thread from 30 seconds before noon, with
  retries stopping 3 seconds before it, so weather can never delay the books
  (a socket timeout limits each read, not a whole response). Only a response
  *received* by noon is the decision snapshot. A city without one is saved
  but marked `weather_late` (no answer by noon, including a success that
  finished after noon), `weather_failed` (the service answered with an error)
  or `weather_empty` (no usable temperatures). Late data is kept as
  `nws_observations_late.json`, labelled `audit_only`, and never scored.
  Threads still running 60 seconds after the books are abandoned. Late books
  outrank weather as the reason. Noon captures have their own bookkeeping, so
  the day-before and noon snapshots for the same day both survive, and noon
  misses are listed separately (`noon_missed`).
- **A run is never lost silently.** Saving to GitHub is retried, including
  GitHub's own "request timed out" answer, and then the whole save is retried
  after 30, 90 and 180 seconds. If every attempt fails, the run prints all its
  files to the log as numbered `UNSAVED_RUN` lines.
  `python -m recorder.unsaved <log file> [--commit]` puts them back together,
  checks each file against run.json, and saves the run folder. It never
  restores `state/state.json`, because by then the recorder has moved on. The
  log line for every run also gives the reason for any failed capture
  (`failed_reasons`, `noon_failed_reasons`).
- **Paper fills need a complete delayed book.** Paper fills are priced on the
  second snapshot, so a city is `paper_fill_eligible` only if its first
  snapshot is eligible and the second arrived on time with every range
  (`complete_recheck`; otherwise `recheck_failed`, `missing_recheck_books`,
  `late_recheck` or `first_snapshot_ineligible`). The first snapshot alone can
  still support research. This applies to both capture kinds.
- **Two different clocks.** The 6 p.m. cutoff follows daylight saving. The day
  the contract measures does not: it is midnight to midnight local *standard*
  time, so 1 a.m. to 1 a.m. on the clock in summer. Both are written to
  `run.json`.
- **Failures are kept.** A failed or partial capture is saved and marked
  `failed`, with every attempt listed.
- **Nothing is overwritten.** Every run writes to a new folder.

### First-run live check

The first run against an empty data repository also fetches every source once
(Kalshi series, markets and order books, NWS forecasts, climate reports) and
saves the results under `checks/`. If anything fails, the run reports
`"ok": false` with the reasons, so a broken connection shows up hours before
the first real cutoff. The check never counts as a capture and is never used
for scoring. It runs once; delete `live_check` from `state/state.json` to run
it again.

### Not built yet

- The weather.com page Kalshi names as its settlement source is not captured.
  Kalshi's own settled result is saved instead, with the NWS report as a check.
- No forecasting model, no fee or paper-trading calculations in the recorder.

## Independent checks of historical data: `verify/`

ChatGPT collects past data into `history/` in the data repository. Claude checks
it independently, without trusting the collector's own manifest.

- `python -m verify.check_history <data dir> <batch>`: offline checks of one
  batch (hashes, the date split, cutoffs and climate days recomputed with the
  recorder's own code, payouts, quote selection, forecast provenance).
- `python -m verify.refetch`: runs hourly at minute 25 as a second Render job.
  For every batch without a report, it re-downloads the Kalshi data and a sample
  of the NOAA forecast files, compares them with what was saved, checks that no
  later forecast cycle was complete before the cutoff, decodes the samples with
  pygrib if installed, and writes a report under `verification/` in the data
  repository. Holdout reports never compare forecasts with outcomes.

## Setting it up on Render

This has not been run on Render yet. The steps below are the intended path.

1. **Create a GitHub token for the data repository.** On GitHub: Settings →
   Developer settings → Personal access tokens → Fine-grained tokens →
   Generate new token. Under *Repository access* choose *Only select
   repositories* and pick `prediction-engine-data`. Under *Repository
   permissions* set **Contents** to *Read and write*. Copy the token.
2. **Create the job on Render.** New → Blueprint, and connect this repository.
   Render reads `render.yaml` and asks for `GITHUB_TOKEN`. Paste the token.
3. **Check the first run.** Open the job's logs. Each run prints one line of
   JSON. `"ok": true` means it worked. New folders appear in the data
   repository after the first cutoff (6 p.m. Eastern is the earliest each day).

Render charges for cron jobs: about $1 a month minimum for this one.

Never put a Kalshi password or API key in either repository. The recorder
does not need one.

## Running the tests

```
python -m unittest discover -s tests -t .
```

The tests replay real Kalshi and NWS responses saved on 2026-11-10
(`tests/fixtures/`), so they need no network. Python 3.10 or newer, no
packages beyond `requirements.txt`.

To try a run without GitHub, set `RECORDER_LOCAL_DIR=/some/folder` and run
`python -m recorder.run`. Files are written to that folder instead.
