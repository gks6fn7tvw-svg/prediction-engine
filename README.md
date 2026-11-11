# prediction-engine

Research code for testing whether a weather model can out-forecast Kalshi's
daily high-temperature markets. Nothing here trades, places orders, or uses a
Kalshi account. No forecasting or trading edge has been shown yet.

## What exists so far: the recorder

`recorder/` is a data logger. It only reads public data and saves it, so that
forecasts and prices can be compared later against what actually happened.

It covers five cities: New York (KNYC), Chicago Midway (KMDW), Miami (KMIA),
Denver (KDEN) and Los Angeles (KLAX).

It runs once an hour, at minute 52. On each run it does three things.

1. **Cutoff capture.** If a city's 6 p.m. local time is 8 minutes away, it
   fetches that city's market rules and the National Weather Service forecast
   *before* 6 p.m., waits until exactly 6 p.m., takes a snapshot of the order
   books for tomorrow's temperature ranges, and takes a second snapshot at
   least one second later.
2. **Settlement.** After a day's weather is over, it checks Kalshi each hour
   until the result is final, saving every change along the way.
3. **Climate reports.** It saves the NWS daily climate report for each city
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
settlement/<event>/<run id>.json             each change in an event's settlement status
cli/<city>/<run id>.html                     each new version of the NWS climate report
state/state.json                             the recorder's own bookkeeping
```

Responses are stored byte-for-byte as received. Values that `run.json` works
out (best bid and ask, the forecast's hourly maximum) are for a quick look
only. The raw files are the record.

### Rules it follows

- **The cutoff is real.** The books are requested at 6 p.m. local time and the
  number of seconds late is recorded. A cutoff more than 20 minutes gone is
  recorded as `missed`, never captured late and passed off as on time.
- **Two different clocks.** The 6 p.m. cutoff follows daylight saving. The day
  the contract measures does not: it is midnight to midnight local *standard*
  time, so 1 a.m. to 1 a.m. on the clock in summer. Both are written to
  `run.json`.
- **Failures are kept.** A failed or partial capture is saved and marked
  `failed`, with every attempt listed.
- **Nothing is overwritten.** Every run writes to a new folder.

### Not built yet

- The weather.com page Kalshi names as its settlement source is not captured.
  Kalshi's own settled result is saved instead, with the NWS report as a check.
- No forecasting model, no fee or paper-trading calculations.

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
