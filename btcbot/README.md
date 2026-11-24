# btcbot: Kalshi 15-minute bot

A bot for Kalshi's 15-minute Up/Down markets (Bitcoin by default). Every rule
is a setting you can change, like the Moku Bot screen. It starts in **paper
mode**, which uses pretend money. It only places real orders after you switch
it to live mode on purpose.

**Honest note:** no setting here has been shown to beat Kalshi's fees. Let
paper mode run for at least 200 trades, then check the score before you use
real money. Paper testing is free, and running longer adds no cost. The score
report includes a verdict that says whether the result is bigger than luck.

## What each setting does

All settings live in `btcbot/settings.yaml`. Prices are in dollars, so
`0.70` means 70 cents, and `null` means off.

| Moku screen | Setting | Starts at |
|---|---|---|
| How much of your Kalshi it can use | `balance_share_pct` | 50% |
| What it buys | `buy`, `min_price`, `max_price` | favorite, 70c to 85c |
| Extra conditions | `conditions:` (spread, contracts offered, rising, distance from target) | all off |
| When it may act | `act_from_minutes_left`, `act_until_minutes_left` | 8 to 7 minutes left |
| How it gets out | `take_profit`, `stop_loss` | off, so it holds to the end |
| Second play | the second block under `plays:` | off |
| Size and limits | `size`, `daily_loss_limit`, `max_trades_per_day` | 1 contract, stop after losing $5 in a day |

Each play buys at most once per market. Orders either fill right away at the
price on screen or cancel. They never sit and wait.

## Set up the free Google Cloud server

Do this once. A computer is easier, but a phone works.

1. **Sign up** at cloud.google.com/free. Google asks for a card, but the
   server below stays free if you stick to these steps.
2. **Set a spending alert:** open Billing, then Budgets & alerts, then Create
   budget. Set it to $1 so Google emails you if anything ever costs money.
3. **Make the server:** open Compute Engine, then VM instances, then Create
   instance.
   - Name: `kalshi-bot`
   - Region: `us-west1 (Oregon)` (closest free region to Hawaii)
   - Machine type: `e2-micro`
   - Boot disk: click Change, pick **Debian**, set the disk type to
     **Standard persistent disk** (the other types are not free), and set the
     size to 10 GB.
   - Click Create.
4. **Open the server:** click **SSH** next to `kalshi-bot`. A black terminal
   window opens.
5. **Install the bot.** Paste these lines into that window and press Enter:
   ```
   sudo apt-get update && sudo apt-get install -y git python3-venv
   git clone -b btcbot https://github.com/gks6fn7tvw-svg/prediction-engine.git
   cd prediction-engine
   python3 -m venv .venv
   .venv/bin/pip install -r btcbot/requirements.txt
   cp btcbot/settings.example.yaml btcbot/settings.yaml
   .venv/bin/python -m btcbot check
   ```
   `check` shows the current market and what each play would do right now.
   It never places an order.

   The `-b btcbot` part is needed only until pull request #8 is merged. After
   it's merged, run `git checkout main && git pull` once on the server, then
   restart the bot.
6. **Keep it running all the time:**
   ```
   bash btcbot/deploy/install_service.sh
   ```
   It now runs in paper mode around the clock, even after you close the window.

### If SSH won't connect: set it up without SSH

The server can install and start the bot by itself every time it boots.

1. Open the server's page, tap **Edit**, and find **Automation**, then
   **Startup script**. It's under **Advanced** (called **Management** on
   some screens).
2. Paste these two lines and tap **Save**:
   ```
   #!/bin/bash
   (curl -fsSL https://raw.githubusercontent.com/gks6fn7tvw-svg/prediction-engine/btcbot/btcbot/deploy/gce_startup.sh || curl -fsSL https://raw.githubusercontent.com/gks6fn7tvw-svg/prediction-engine/main/btcbot/deploy/gce_startup.sh) | bash
   ```
3. On the server's page, tap **Reset** to restart it.
4. Wait about 5 minutes. Then open **Logs**, then **Serial port 1
   (console)**, and scroll to the bottom. Look for lines that start with
   `BTCBOT`: the check of the current market, then
   `DONE. The bot is running.` The bot's own log lines appear there too.

To change settings without SSH: on **Edit**, under **Metadata**, add a key
named `btcbot-settings`, paste your whole settings file as the value, save,
and tap **Reset**. On every restart the server also downloads the newest
code.

Google's free trial runs 90 days. When Google asks, upgrade to a full account
to keep the server. It stays free as long as you use only this one small server.

## The bot's website (phone friendly)

The bot comes with a small private website.

- **Overview:** running or not, balance, today and all-time profit, the live
  market (price to buy Up and Down, minutes left), a card for each play, recent
  activity, Pause, and "Get the newest version".
- **Each play's page:** its score (bets, win rate, the win rate it needs,
  profit, verdict), its open bets with what they could sell for right now, its
  closed bets, and its own activity.
- **Settings:** every setting. Plays can be added, copied, renamed and deleted,
  and each play can have its own bet size (or use the default). Saved settings
  are checked first; the bot then restarts with them in about 15 seconds and
  keeps its open bets.
- **Get the newest version** downloads new code and restarts the bot and the
  site, so updates don't need Google Cloud.

For plays that sell early (take profit or stop loss), the win rate they need is
worked out from their average win and average loss, not from the price paid.

On the Google Cloud server (startup-script setup):

1. On the server's page tap **Edit**, tick **Allow HTTP traffic**, and **Save**.
2. Tap **Reset**. After about 5 minutes open **Logs > Serial port 1** and
   search for `BTCBOT WEBSITE`. That line is your private link.
3. Open it on your phone and add it to the home screen. The link logs the phone
   in for a year, so the home-screen icon keeps working.

Keep the link private: anyone with it can change settings. The site is plain
http, so for safety it can't switch the bot to real money, and it never sees
Kalshi keys. The link changes only if the server is stopped and started (its
address changes); a Reset keeps it. Each boot prints it again.

Elsewhere: `python -m btcbot web --port 8080` runs it on any computer; the key
is made in `btcbot/data/web.key` (or the file named by `BTCBOT_WEB_KEY_FILE`).

## Everyday use (SSH)

Open the SSH window, then run `cd prediction-engine` first.

| To... | Type |
|---|---|
| Watch it live | `journalctl -u btcbot -f` (Ctrl+C stops watching, not the bot) |
| See the score and verdict | `.venv/bin/python -m btcbot score` |
| Change settings | `nano btcbot/settings.yaml`, save with Ctrl+O, Enter, Ctrl+X, then `sudo systemctl restart btcbot` |
| Pause new trades | `touch btcbot/data/paper/STOP` (for live: `btcbot/data/live/STOP`) |
| Resume | `rm btcbot/data/paper/STOP` |
| Stop the bot | `sudo systemctl stop btcbot` |
| Get the newest code | `git pull && sudo systemctl restart btcbot` |

The trade list is `btcbot/data/paper/trades.csv`. Paper and live keep separate
folders, so the scores never mix. To restart paper testing from zero, delete
`btcbot/data/paper` and restart the bot.

## Going live (real money)

Only do this after a play's score says "ahead by more than luck" over at least
200 bets, including weekdays. Only bet money you're OK losing.

**The locked link (https).** The website's plain link isn't encrypted, so the
Kalshi key is only accepted over a locked https link. The website gets one by
itself: a free Let's Encrypt certificate for the server's IP address (6-day
certificates, renewed automatically when 3 days are left; certbot runs as the
bot's user and proves the IP through the website on port 80). One time only, in
Google Cloud: open the server, tap **Edit**, tick **Allow HTTPS traffic**, Save.
The Overview then shows "A locked link is ready"; open it and add it to the home
screen.

**Why subaccounts:** on Kalshi, each account holds one side per market. If one
play holds Up and another buys Down in the same 15 minutes, Kalshi cancels them
against each other. So with real money every play that's on gets its own Kalshi
**subaccount** (its own money and bets). Subaccount 0 is your main account.
Subaccounts are made through the API only; the Kalshi app shows only the main
account, and the bot's website shows them all. Making them needs Kalshi's
Advanced API level, which Kalshi grants for free once one of your last 100 orders
came from the API; the bot asks for it by itself.

**Money pots:** Kalshi splits each account's cash into pots by kind of market
("exchange shards": crypto is 2, sports 3). Orders from the app or website move
money between pots for you; orders from a bot don't, so the crypto pot must
already hold the money. The Overview shows each pot. Tap **Move $X into the
crypto pot** to move the main account's other pots over (Kalshi's Intra Account
Transfer; it runs in the background and the bot confirms it arrived). If the
pot is short, the log says so instead of trying anyway.

**Step 1: one play, main account, tiny bets** (this also qualifies you for subaccounts)

1. **Make a Kalshi API key:** on kalshi.com, open your profile settings, find
   **API Keys**, and click **Create New API Key**. Keep the Key ID and the key
   file; the file downloads only once.
2. On the locked link, open **Settings → Real money**, paste the Key ID and the
   whole text of the key file, and tap **Save key**. It is checked with Kalshi
   (you'll see your balance) and kept on the server where only the bot can read
   it. The website never shows it again.
3. Turn every play off except **main**: 1 contract, Kalshi subaccount **0**,
   daily loss limit **$5**.
4. Type `I accept real-money risk` and tap **Switch to real money**. Compare its
   first bets with the Kalshi app's history.

**Step 2: subaccounts for every play**

1. Give each play you want on its own subaccount number (main 1, cheap 2, ...).
   Leave 0 for your own bets in the Kalshi app.
2. Save. The bot restarts, upgrades the API level, and makes the subaccounts.
3. On the Overview, under **Kalshi money**, move a little money from your main
   account into each play's subaccount. Moves stay inside your Kalshi account.
4. Turn the plays on one at a time.

(Without the website, the key can also come from the server's metadata:
`kalshi-key-id`, `kalshi-private-key`, `btcbot-mode` = `live` or `paper`, then Reset.)

**Safety check:** with real money, every 10 seconds the bot compares its open
bets with what Kalshi shows in each subaccount (markets still open only). If they
disagree twice in a row, it pauses new bets and shows a red box saying where.
Check the Kalshi app, then tap **Resume**. Don't place bets by hand in a
subaccount a play uses.

To go back to pretend money, tap **Switch back to pretend money** on Settings.

## Goal mode (automatic bet sizes)

Settings → **Goal mode**: tick it, set the goal (say $100), how bold, a stop
amount, and the biggest single bet. Goal mode replaces the bet sizes:

- Each play bets **1 contract** until its record (pretend and real bets
  together) has at least 30 bets and shows an edge even when its win rate is
  counted cautiously (the low end of its luck range).
- Then it bets the share of its money that grows it fastest for that edge (the
  Kelly rule): **careful** = 1/4 of it, **normal** = 1/2, **bold** = all of it
  (much bigger swings). No bet goes over the "biggest bet" share.
- Bets grow as the money grows and shrink after losses. With pretend money the
  pot is shared evenly by the plays that are on; with real money each play grows
  its own subaccount.
- At the goal, or below the stop amount, new bets pause and the Overview says
  why. Raise the goal and tap Resume to keep going.

It can't make a play without a real edge reach the goal; it only stops a good
play from betting too little or too much. Try it with pretend money first.

## Live score (1-10 for Up and Down)

The price card shows a score for each side, refreshed at least every 5 seconds (every
second in the last minutes). It compares **our chance** with **Kalshi's price**:
5 = the price is fair, higher = that side is cheap for its chance, lower = expensive.

Our chance (`lastminute.chance_up`): how far Bitcoin is from the target, how jumpy it has
been (Coinbase 1-minute candles, last 30 minutes, refreshed each minute), how much time is
left, and in the final minute how much of the settlement average is already counted (the
running estimate above). The badge sits on the side we think will win, as score plus a
small number for **how sure that side is to win** (price doesn't change it). From the
backtest of 1,328 Bitcoin markets (Nov 9-23 2026), picks in each band won:
¹ under 70% → about 6 in 10 · ² 70-90% → about 8 in 10 · ³ 90%+ → about 19 in 20.
Being steady for minutes or "pulling away" didn't change how often a pick won once the
chance was known, so they aren't part of it. Example: 8² = likely winner at a good price;
5³ = almost sure but no bargain (the price already says so).
It assumes moves are random, so news or one huge order can still
beat it. Snapshots at 10, 5, 4, 3, 2, 1 minutes and 30 seconds left are checked after each
close (`scores.csv`, "Score track record" on the Overview). On the first night of data the
same math was about as accurate as Kalshi's own price in the last 5 minutes, and better
near the end, on only 35 markets.

**Each second counts once.** The last-minute average holds the latest price seen at each
whole second (Kalshi averages one price per second; the bot checks every 1-5 s, unevenly),
so a burst of checks doesn't count extra. The second-by-second file (`lastminute/*.csv`)
also records how many contracts sit at the best prices (`yes_bid_size`, `yes_ask_size`) and
our chance and scores each second, to test whether a 7+ deal lasted 1, 3 or 5 seconds.

**Live prices.** Kalshi's market list only refreshes its prices about every 30 seconds
(the bot's own records showed the price changing at most twice a minute while Kalshi's
history showed more moves). For the market that's running, the bot now reads the live
order book every step (`KalshiClient.best_prices`), so the website, the score and the
plays all use the current price. If Kalshi refuses the order book (401/403), it falls
back to the list's prices and says so in the log.

## The deal play (our pick + score)

Two settings make it: **What it buys → Our pick** (the side our chance says will win) and
**Only buy if its live score is at least 7** (under Extra conditions). The button
"+ Add the deal play (pretend money)" sets it up: 5 to 1 minutes left, 1 contract, any price.

Each play can also choose **Levels it buys at** (tick ¹ ² ³; all ticked = any) and
**Buys per market** (more than 1 = it buys again, same side, at least `seconds_between_buys`
apart, default 60, while the bet is still open and the rules still pass). If the pick
switches sides it stops adding: buying both sides in one account cancels out on Kalshi.
The website's trade box blocks the same thing (hold Up → Buy Down says "Sell now first").

Backtest of the last 5 minutes, our pick, buying at each minute mark that qualifies (up to
3 per market): score 7+ any level 94 bets +$12.77 · level ² only 33 bets, won 9 in 10,
+$6.79 · ² and ³ 57 bets +$8.47 · score 6+ about even (too loose) · 8+ 40 bets +$9.70.

Why: in the backtest, buying the likely winner at Kalshi's price lost a little at every
level (Kalshi's price already knows who's likely to win, and the fee takes the rest).
Main (favorite 70-85¢ at 8-7 minutes) lost $16.53 over 642 bets. The one rule that made
money was our pick with a score of 7+ in the last 5 minutes: 73 bets, won about 3 in 4 at
about 62¢, +$7.49, up in both weeks. Only 2 weeks of minute snapshots, so it starts with
pretend money.

## Backtest data (Settings → Collect history)

Downloads the last N days of settled markets (`btcbot/history.py`): Kalshi's 1-minute Up
bid/ask candles per market (batch endpoint, split to stay under 10,000 periods per
request; falls back to the per-market and historical endpoints), each result and official
settlement value (`expiration_value`), Coinbase 1-minute candles for the same minutes, and
Binance's per-minute buying power (`buy_volume` = coins bought by takers, `all_volume` =
everything traded; daily files from data.binance.vision, the newest day from Binance.US;
`flow_from` says which).
One row per market-minute in `history.csv` (download link on Settings). Runs in the
background in the website process; public data only. With a saved key it also checks
whether the account may read the official BRTI through Kalshi's CF Benchmarks passthrough.

## Make a trade (from the website)

The Overview has a **Make a trade** box under the prices (tap a price to jump there):
contracts, an optional "most I'll pay", and **Buy Up** / **Buy Down**. The website only
writes a request; the bot (which wakes within a quarter second) buys in the market closing
next, at the price on screen plus up to 2 cents (or your limit), from the main account's
crypto pot, and records it as the play `manual` so the records check stays right. Each
open manual bet has a **Sell now** button. Requests older than 15 seconds are dropped.
With real money this works only on the 🔒 locked link (and asks to confirm).

## Pretend-only plays (beside real money)

Tick **🧪 Pretend money only** on a play (or `pretend: true`) to test it with pretend
money while the other plays use real money. The bot then runs two sets of books in one
loop: the real plays (live folder, Kalshi orders) and the pretend-only plays (paper
folder, `PaperBroker`, its own pretend money and daily limits). Pretend-only plays never
send an order, need no subaccount, and their results never count toward the real totals.
Their log lines start with "pretend money ·". The Pause button stops both; "Reset pretend
money" on Settings resets the pretend-only plays' money.

## Last-minute watch (no orders)

Kalshi settles each 15-minute market on the simple average of the 60 once-a-second
CF Benchmarks index prices (BRTI) in the final minute, compared with the target. Part of
the answer is locked in as that minute passes, so fast traders reprice before Kalshi's
page catches up. The bot keeps its own running estimate (`btcbot/lastminute.py`): the
median of Coinbase, Kraken and Bitstamp mid prices (three of the index's seven
exchanges; the index itself is a paid feed), averaged over the seconds already counted,
with the current price filling the rest.

It only watches. Two ideas are written down and checked after each close:
- **Guard:** a bet you hold, when the estimate turns at least $X against it in the last
  N seconds: "would sell now at the bid". Compared with holding to the end.
- **Follow:** when the estimate is at least $X past the target in the last N seconds and
  that side costs at most Y cents: "would buy 1". Win or loss after fees.
- **Copy the crowd:** a bet you hold whose Kalshi price falls to Y cents or less in the
  last N seconds (others are selling it): "would sell now at the bid". Uses Kalshi's own
  price, where the fast traders' moves show up, instead of our estimate.

Results: `watch.csv` (Overview: "Last-minute watch"); every second of the final stretch:
`lastminute/<day>.csv` (two weeks kept). Settings: `last_minute:` or the website.
Limits: the stand-in isn't the official index, the bot checks once a second from one
server (the fastest traders react in well under a second), and a past pattern can fade.

## For reviewers (ChatGPT)

- API details were checked against docs.kalshi.com on 2026-11-21: host
  `external-api.kalshi.com/trade-api/v2`, RSA-PSS signing over
  timestamp + method + path without the query string, and Create Order V2
  (`POST /portfolio/events/orders`, side `bid`/`ask` on the YES leg). Exits use
  `reduce_only`, which requires `immediate_or_cancel`.
- Buying Down at P is sent as a YES `ask` at 1 - P. The fill price is assumed
  to come back as the YES-leg price and is converted to 1 - P. `broker.py`
  checks this against the limit and logs a warning if Kalshi reports the
  Down price directly. **Please confirm on the first 1-contract live trade.**
- The paper fee is 0.07 x C x P x (1 - P) x the series multiplier, rounded up
  to the cent per order. Paper fills happen only at the best price on screen,
  and only up to the size offered there.
- The settlement result comes from `GET /markets/{ticker}` (`result` yes/no).
  "Up" is treated as YES. Please confirm on kalshi.com that KXBTC15M's YES
  side means "Up".
- Subaccounts (checked 2026-11-22): orders send `subaccount` (omitted for 0);
  `GET /portfolio/positions` and `GET /portfolio/balance` take `subaccount`;
  cash per subaccount comes from `GET /portfolio/subaccounts/balances` (one
  call, since `/portfolio/balance` is limited to 1 call a second on Basic and
  Advanced); `POST /portfolio/subaccounts` makes the next number;
  `POST /portfolio/subaccounts/transfer` moves cash (`client_transfer_id`,
  `from_subaccount`, `to_subaccount`, `amount_cents`); `POST
  /account/api_usage_level/upgrade` grants Advanced when one of the last 100
  orders came from the API. Netting settings are left as Kalshi has them.
  **Please check:** the Create Subaccount reply field name (code accepts
  `subaccount_number` or `subaccount`), and that netting doesn't merge
  positions across subaccounts.
- Goal mode (`goal.py`): Kelly share p/L - q/W from each play's record, with p
  at its Wilson lower bound (z = 1), at least 30 bets, scaled by boldness and
  capped; unproven plays bet 1 contract. Please sanity-check the math.
- Locked link (`secure.py`): certbot >= 5.4 `certonly --preferred-profile
  shortlived --webroot --ip-address <IP>` as the bot user, renewed with
  `--force-renewal` when under 3 days remain or the IP changes; served on 443 by
  the same handler. The Kalshi key form and the real-money switch work only
  there; keys are saved 0600 in ~/.btcbot.
- Live settings refuse two enabled plays on one subaccount. The records check
  (`Bot.check_records`) compares net positions per subaccount for open markets
  every 10 s and pauses after two disagreements in a row.
- Tests: `python -m unittest discover -s tests`. They run offline with fake
  markets and a pretend Kalshi exchange that keeps subaccounts apart. A 200-market simulation also checked that paper cash equals the
  starting balance plus every trade's profit.
