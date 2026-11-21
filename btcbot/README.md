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

Google's free trial runs 90 days. When Google asks, upgrade to a full account
to keep the server. It stays free as long as you use only this one small server.

## Everyday use

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

Only do this after `score` says "ahead by more than luck" over at least 200 trades.

1. **Make a Kalshi API key:** on kalshi.com, open your profile settings
   (kalshi.com/account/profile), find **API Keys**, and click **Create New
   API Key**. Write down the **Key ID**. The private key file downloads only
   once, so keep it safe and never share it.
2. **Put the key file on the server:** in the SSH window, use the
   **Upload file** button at the top and upload the key file. Then rename it
   and lock it, using your file's real name:
   ```
   mv ~/YOUR-KEY-FILE-NAME ~/kalshi_key.pem && chmod 600 ~/kalshi_key.pem
   ```
3. **Tell the bot about the key**, using your real Key ID:
   ```
   echo "KALSHI_API_KEY_ID=YOUR-KEY-ID" >> ~/.btcbot.env
   echo "KALSHI_PRIVATE_KEY_PATH=$HOME/kalshi_key.pem" >> ~/.btcbot.env
   ```
4. **Switch the settings:** in `btcbot/settings.yaml`, set `mode: live`
   and `live_confirm: "I accept real-money risk"`.
5. **Test the key without trading:**
   ```
   set -a; . ~/.btcbot.env; set +a; .venv/bin/python -m btcbot check
   ```
   It should print your Kalshi balance.
6. **Restart:** `sudo systemctl restart btcbot`. The log will say LIVE MODE.

To go back to pretend money, set `mode: paper` and restart.

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
- Tests: `python -m unittest tests.test_btcbot`. They run offline with fake
  markets. A 200-market simulation also checked that paper cash equals the
  starting balance plus every trade's profit.
