# Hosting the 14-day paper test

P1 ([EXPERIMENTS.md](EXPERIMENTS.md#p1-the-14-day-paper-test-of-the-maker-bot-frozen-2026-10-23))
needs two programs running for 14 days without a long break:

| Program | Command | Writes to |
|---|---|---|
| Paper bot | `python -m kbot --config config.maker.toml run` | `data/paper-maker.sqlite` |
| Recorder | `python -m kbot record` | `data/prospective.sqlite` |

Both are paper only and read public market data with no key. Neither reads `.env`. A break over
10 minutes adds its length to the test. A break over 2 hours voids it, and P1 starts again on a
fresh database.

## Can your Mac do it?

Use the Mac only if every line is true for the whole 14 days:

- It is a desktop Mac, or a MacBook that stays plugged in with the lid open.
- It stays plugged in to power.
- You stay logged in. The jobs run inside your login session. Logging out stops them. Locking the
  screen is fine.
- After a power cut or a forced restart, you can unlock it within 2 hours. With FileVault on, a
  restarted Mac waits at the password screen and nothing runs until you type it.
- Automatic macOS updates are off, since they restart the Mac.
- The internet connection is steady. Short Wi-Fi drops are fine: the bot stops quoting while its
  data is stale and starts again by itself.

If one of these fails, use the server below. It costs about $3 for the 14 days.

## Mac setup (once)

1. Put the repo in your home folder, not in Documents, Desktop, Downloads or iCloud. macOS blocks
   background jobs from reading those folders, and the install script refuses them.

   ```bash
   cd ~
   git clone https://github.com/tarheelfanjr-ux/kalshi-bot.git   # private: sign in when git asks
   cd ~/kalshi-bot
   scripts/setup.sh          # needs Python 3.11+. If it says too old: brew install python@3.12,
                             # then PYTHON=python3.12 scripts/setup.sh
   ```

2. Stop sleep on power and restart after a power cut:

   ```bash
   sudo pmset -c sleep 0 disksleep 0
   sudo pmset -a autorestart 1      # desktops. Some laptops ignore it
   pmset -g                         # check: "sleep 0" in the AC Power section
   ```

   Then in System Settings:
   - Energy (desktop) or Battery > Options (laptop): turn on "Prevent automatic sleeping when the
     display is off" and, on a desktop, "Start up automatically after a power failure".
   - General > Software Update > Automatic Updates: turn off "Install macOS updates".

3. Start from a fresh P1 database. Skip this on a new clone, which has none:

   ```bash
   mkdir -p data/old && mv data/paper-maker.sqlite* data/old/ 2>/dev/null; true
   ```

4. Install and start both jobs:

   ```bash
   scripts/macos/install-agents.sh
   ```

   launchd starts them now and at every login, and restarts either one 30 seconds after it exits.
   Each runs under `caffeinate -s`, which keeps the Mac awake while it is on power.

5. Check after 2 minutes:

   ```bash
   .venv/bin/python -m kbot --config config.maker.toml status   # P1 line says "collecting: day 0 of 14"
   .venv/bin/python -m kbot experiment status                   # recorder_heartbeat_age_s under 5
   tail -n 20 logs/paper-maker.log logs/recorder.log            # warnings and errors only
   ```

### Restart, stop, remove (Mac)

| To | Run |
|---|---|
| Restart the bot | `launchctl kickstart -k gui/$(id -u)/com.kbot.paper` |
| Restart the recorder | `launchctl kickstart -k gui/$(id -u)/com.kbot.recorder` |
| Stop both until the next login | `launchctl bootout gui/$(id -u)/com.kbot.paper; launchctl bootout gui/$(id -u)/com.kbot.recorder` |
| Start both again | `scripts/macos/install-agents.sh` |
| Remove both | `scripts/macos/install-agents.sh remove` |
| Is it running? | `launchctl print gui/$(id -u)/com.kbot.paper \| grep -E "state =\|pid ="` |

After a reboot, log in. Both jobs start by themselves.

## Server option: a $6 a month Linux server

The simplest low-cost choice is a DigitalOcean Basic droplet: 1 GB memory, 1 vCPU, 25 GB disk,
$6 a month, billed by the hour ([plan details](https://vpsbenchmarks.com/hosters/digitalocean/plans/std_1gb_1core)).
Amazon Lightsail's 1 GB plan is $7 a month and works the same way
([pricing](https://aws.amazon.com/lightsail/pricing)). Hetzner is no longer cheap in the US: its
smallest US server is about $21 a month after its 2026 price changes
([prices](https://agentdeals.dev/hetzner-pricing-2026)). Check the price on the order page.

1. Create the server: Ubuntu 24.04, a US region (New York or San Francisco), SSH key login.
2. Log in and check that Kalshi answers from it. If this fails, delete the server and try another
   region:

   ```bash
   ssh root@SERVER_IP
   curl -s https://api.elections.kalshi.com/trade-api/v2/exchange/status
   ```

3. Make a user for the bot and keep its services running without a login:

   ```bash
   apt update && apt install -y git python3 python3-venv
   ufw allow OpenSSH && ufw --force enable          # nothing else is reachable from outside
   adduser --disabled-password --gecos "" kbot
   loginctl enable-linger kbot
   mkdir -p /home/kbot/.ssh && cp ~/.ssh/authorized_keys /home/kbot/.ssh/ && chown -R kbot:kbot /home/kbot/.ssh
   timedatectl | grep synchronized                  # must say yes: the bot halts on clock skew over 2 s
   exit
   ```

4. Install as that user. The repo is private: when git asks for a password, paste a GitHub
   fine-grained token with read-only access to this repo into the terminal, never into a chat.

   ```bash
   ssh kbot@SERVER_IP
   git clone https://github.com/tarheelfanjr-ux/kalshi-bot.git ~/kalshi-bot
   cd ~/kalshi-bot && scripts/setup.sh
   mkdir -p ~/.config/systemd/user && cp scripts/linux/kbot-*.service ~/.config/systemd/user/
   systemctl --user daemon-reload
   systemctl --user enable --now kbot-paper kbot-recorder
   ```

5. Check after 2 minutes, the same way as on the Mac:

   ```bash
   .venv/bin/python -m kbot --config config.maker.toml status
   .venv/bin/python -m kbot experiment status
   ```

### Restart, stop, logs (server)

| To | Run (as kbot) |
|---|---|
| Restart the bot or the recorder | `systemctl --user restart kbot-paper` or `kbot-recorder` |
| Stop both | `systemctl --user stop kbot-paper kbot-recorder` |
| Start both | `systemctl --user start kbot-paper kbot-recorder` |
| Is it running? | `systemctl --user status kbot-paper kbot-recorder` |
| Logs | `journalctl --user -u kbot-paper -n 50` |
| See the dashboard on your Mac | `ssh -L 8765:127.0.0.1:8765 kbot@SERVER_IP "cd kalshi-bot && .venv/bin/python -m kbot --config config.maker.toml dashboard"`, then open http://127.0.0.1:8765 |

Both services start again after a crash (30 s) and after a server reboot, with no login.

If `systemctl --user` says it cannot connect to the bus, run
`export XDG_RUNTIME_DIR=/run/user/$(id -u)` and try again.

## What a restart does

On every start, each program compares the time with its last heartbeat and logs an `interruption`
event with the downtime. The bot also logs `unexpected_termination` if the last run did not shut
down cleanly. It then reads the public trade tape back to its oldest open quote, so fills that
happened while it was down are not lost, and cancels quotes that expired in the meantime. Its first
order after a restart may be rejected for a stale book while it catches up. That costs one minute
of quotes and is logged.

`status` shows P1's gaps over 10 minutes, the longest gap, the restarts, the longest downtime and
the halted hours. `experiment status` shows the recorder's restarts and its longest downtime.

## During the 14 days

- Look at `status` once a day.
- A risk halt (5 losses in a row, or the daily loss limit) stays on until you clear it. Read why,
  then `python -m kbot --config config.maker.toml resume --reviewed "what I checked"`.
- Do not edit `config.maker.toml`, save settings in the web page, or pull new code. A change to the
  limits shows in `status` and fails the test.
- Do not start a second copy by hand while the service runs. It refuses to start on the same
  database, and the web page hides Start and Stop when the services are installed.
- Keys stay out of chat and out of Git. Any key goes in `.env`, which Git ignores. Run
  `chmod 600 .env`. P1 itself needs no key.
