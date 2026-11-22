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
200 bets, including weekdays.

**Why subaccounts:** on Kalshi, each account holds one side per market. If one
play holds Up and another buys Down in the same 15 minutes, Kalshi cancels them
against each other. So with real money every play that's on gets its own Kalshi
**subaccount** (its own money and bets). Subaccount 0 is your main account.
Subaccounts are made through the API only; the Kalshi app shows only the main
account, and the bot's website shows them all. Making them needs Kalshi's
Advanced API level, which Kalshi grants for free once one of your last 100 orders
came from the API; the bot asks for it by itself.

**Step 1: one play, main account, tiny bets** (this also qualifies you for subaccounts)

1. **Make a Kalshi API key:** on kalshi.com, open your profile settings
   (kalshi.com/account/profile), find **API Keys**, and click **Create New API
   Key**. Write down the **Key ID**. The private key file downloads only once;
   keep it safe and never share it.
2. On the website, turn every play off except **main**, set main to
   **1 contract**, its Kalshi subaccount to **0**, and the daily loss limit to **$5**.
3. In Google Cloud, open the server, tap **Edit**, and under **Metadata** add:
   - `kalshi-key-id` = your Key ID
   - `kalshi-private-key` = the whole text of the key file (open it in a text
     app and copy everything, including the BEGIN and END lines)
   - `btcbot-mode` = `live`

   Save, then **Reset**. The serial log says "Kalshi key loaded" and "switched
   to REAL MONEY"; the website shows **REAL MONEY**.
4. Let it place a bet. Compare it with the Kalshi app's history.

**Step 2: subaccounts for every play**

1. On the website, give each play you want on its own subaccount number
   (main 1, cheap 2, ...). Leave 0 for your own bets in the Kalshi app.
2. Save. The bot restarts, upgrades the API level, and makes the subaccounts.
3. On the Overview, under **Kalshi money**, move a little money from your main
   account into each play's subaccount. Moves stay inside your Kalshi account.
4. Turn the plays on one at a time.

**Safety check:** with real money, every 10 seconds the bot compares its open
bets with what Kalshi shows in each subaccount (markets still open only). If they
disagree twice in a row, it pauses new bets and shows a red box saying where.
Check the Kalshi app, then tap **Resume**. Don't place bets by hand in a
subaccount a play uses.

To go back to pretend money, set the metadata `btcbot-mode` to `paper` and Reset.

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
- Live settings refuse two enabled plays on one subaccount. The records check
  (`Bot.check_records`) compares net positions per subaccount for open markets
  every 10 s and pauses after two disagreements in a row.
- Tests: `python -m unittest discover -s tests`. They run offline with fake
  markets and a pretend Kalshi exchange that keeps subaccounts apart. A 200-market simulation also checked that paper cash equals the
  starting balance plus every trade's profit.
