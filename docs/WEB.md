# Web control center

A website for watching and running the bot from a browser. It reads the same database and config
file as the bot, and every button goes through the same code as the CLI (`kbot/control.py`).

It starts the bot in **paper mode only**. It has no way to arm production or place a real order.

## Start it

```bash
scripts/web.sh
```

Your browser opens <http://127.0.0.1:8765>. If it does not, open that address yourself.
Press Ctrl-C in the Terminal window to stop the website. A bot you started from the page keeps
running; stop it from the page first if you want everything off.

The first run sets everything up (about a minute) and runs the tests. It copies
`config.maker.toml` to `config.web.toml`, which is where the Settings page saves. Git ignores that
file, so pulling updates never overwrites your settings. Delete it to start over.

## First-time setup on macOS

1. Open Terminal (Applications, Utilities).
2. Check Python: `python3 --version`. You need 3.11 or newer. If it is older or missing, install
   the current Python from <https://www.python.org/downloads/macos/> (free), then open a new
   Terminal window.
3. Get the project if it is not on your Mac yet. With GitHub Desktop (free): File, Clone
   repository, `tarheelfanjr-ux/kalshi-bot`. Or in Terminal:
   `git clone https://github.com/tarheelfanjr-ux/kalshi-bot.git ~/kalshi-bot`.
4. Go to the folder and start: `cd ~/kalshi-bot`, then `scripts/web.sh`.

No API key, paid service or extra install is needed. Paper trading reads public Kalshi and
Coinbase prices.

## Using it

| Page | What it is for |
|---|---|
| Overview | Is the bot healthy, paper P&L (gross, fees, net), positions, recent decisions and fills |
| Live markets | Open 15-minute markets: real Kalshi prices next to the bot's simulated decisions |
| Positions and trades | Open positions, every decision (taken or skipped) with its reason, simulated fills, settled trades |
| Strategies | Historical and paper results against the no-trade baseline. Nothing is called profitable without out-of-sample proof |
| Backtesting | Run a historical backtest on train or validation. The holdout is locked |
| Bot controls | Start, pause, resume, stop, emergency stop, health and risk limits |
| Settings | Markets, strategy, risk limits. Checked before saving. Applied on the next start |

**Start paper trading**: Bot controls, Start paper trading. It shows Running after its first
heartbeat, a few seconds later.
**Pause entries**: stops new trades and keeps tracking open positions.
**Resume**: asks what you checked, then allows new trades again.
**Stop**: asks first, then shuts the bot down cleanly. Resting paper bids are cancelled. Open
positions settle on Kalshi's schedule.
**Emergency stop**: asks first, then blocks every new order at once. It stays on through restarts.
To clear it, press Resume: it asks you to confirm and to say what you checked.

The status pill in the sidebar tells you whether it is working:

| Status | Meaning |
|---|---|
| Running | Loop is alive and data is fresh |
| Paused | No new entries: you paused it, a risk limit tripped, or the emergency stop is on. The reason is shown |
| Disconnected | The process is alive but has no fresh heartbeat or data. The detail line says why. It usually recovers by itself; if not, Stop and Start |
| Stopped | Not running. "Did not shut down cleanly" means the computer slept, crashed or was shut down while it ran |

Every result carries its kind: HISTORICAL BACKTEST, SIMULATED PAPER TRADING, KALSHI DEMO or REAL
MONEY. Prices on Live markets are real Kalshi data. Fills, positions and P&L are simulated.

## After you restart or shut down your Mac

The bot and the website stop when the Mac shuts down. To bring them back:

1. Open Terminal.
2. `cd ~/kalshi-bot` (or wherever the project is), then `scripts/web.sh`.
3. In the page, Bot controls, Start paper trading.

Everything is kept in the database: trades, positions, pause and emergency-stop flags. The bot
picks up where it left off.

While it runs, the Mac must stay awake. Sleep pauses the data feed and the page shows
Disconnected. To keep the Mac awake while the website runs, start it with
`caffeinate -i scripts/web.sh` instead.

## Security

* It listens on 127.0.0.1 only, so other devices cannot reach it.
* It refuses to listen on any other address unless `KBOT_DASHBOARD_TOKEN` is set to 16 or more
  characters. Then every page needs a login with that token. The session cookie is HttpOnly and
  SameSite=Strict, and Secure behind HTTPS. Ten wrong tokens in ten minutes lock the login for ten
  minutes.
* Every button sends a CSRF token, JSON only, and is refused from another site. The Host header is
  checked, which blocks DNS-rebinding attacks.
* API keys never reach the browser. The website does not need Kalshi keys at all. When it starts
  the bot, it removes trading keys from the bot's environment, so that bot can only paper trade.
* Pages are served with a strict Content-Security-Policy, no framing, and no referrer.

## Hosting it online later

You only need this to reach the bot when your Mac is off. Local use costs nothing.

1. **An always-on machine.** A small Linux cloud server (about $5 to $10 a month from any VPS
   provider), or a spare computer at home that stays on.
2. **Run the bot and website as services**, so they restart after a reboot or crash. On Linux,
   two systemd units:
   ```ini
   # /etc/systemd/system/kbot-web.service
   [Service]
   WorkingDirectory=/home/kbot/kalshi-bot
   EnvironmentFile=/home/kbot/kalshi-bot/.env
   ExecStart=/home/kbot/kalshi-bot/.venv/bin/python -m kbot --config config.web.toml dashboard
   Restart=always
   User=kbot
   [Install]
   WantedBy=multi-user.target
   ```
   Start the bot from the page as usual. To run it as a service too, add a second unit with
   `ExecStart=... -m kbot --config config.web.toml run`; the page then shows and controls it the
   same way (pause, resume, emergency stop; Stop sends it the same signal, and systemd restarts
   it unless you stop the service).
3. **Keep the website on 127.0.0.1 and put a login layer in front.** Two good options:
   * **Tailscale** (free for personal use): only your own devices can reach the server.
     `tailscale serve 8765` gives it an HTTPS address on your private network.
   * **Caddy** with a domain: automatic HTTPS. `bot.example.com { reverse_proxy 127.0.0.1:8765 }`.
     Add Cloudflare Access or similar if you want a second login.
4. **Always set both** in `.env` on the server (`chmod 600 .env`):
   ```bash
   KBOT_DASHBOARD_TOKEN=<a long random string, e.g. from: openssl rand -base64 24>
   KBOT_WEB_ALLOWED_HOSTS=bot.example.com     # the name you open in the browser
   ```
5. **Never** forward port 8765 from your router or open it in a cloud firewall. Never put
   Kalshi keys anywhere but `.env` on the server.

## Limits

* The page starts paper mode only. Demo and production stay CLI steps in [GO_LIVE.md](GO_LIVE.md).
* Settings take effect when the bot next starts. The page says when the running bot is on older
  settings. Raising a risk limit asks for confirmation.
* The maker strategy is not backtested from the page; it needs public trades (`kbot maker-history`).
  One backtest runs at a time.
* Historical results on the Strategies page come from the JSON files in `reports/` and the dataset
  in `data/`. If those are missing on a machine, the page says so instead of showing numbers.
* Start and stop detection uses `ps`, so it works on macOS and Linux, not Windows.
