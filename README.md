# Marzban Telegram Bot

A Telegram bot for managing [Marzban](https://github.com/Gozargah/Marzban) VPN panel subscriptions. Admins can add/remove users and check their stats; registered users can fetch their subscription link on demand.

---

## Prerequisites

- Python 3.11 or newer
- pip
- A running Marzban panel with API access
- A Telegram bot token (create one via [@BotFather](https://t.me/BotFather))

---

## Installation

```bash
git clone https://github.com/yourname/marzban-bot.git
cd marzban-bot
pip install -r requirements.txt
```

---

## Configuration

Copy the example file and fill in your values:

```bash
cp .env.example .env
```

Edit `.env`:

| Variable | Description |
|---|---|
| `TELEGRAM_TOKEN` | Bot token from @BotFather |
| `MARZBAN_URL` | Full URL to your Marzban server, no trailing slash |
| `MARZBAN_USERNAME` | Marzban admin username |
| `MARZBAN_PASSWORD` | Marzban admin password |
| `ADMIN_IDS` | Comma-separated Telegram user IDs that have admin access |

**How to find your Telegram ID:** Message [@userinfobot](https://t.me/userinfobot) — it replies with your numeric user ID.

---

## Running

```bash
python bot.py
```

The SQLite database (`db.sqlite3`) is created automatically on first run.

---

## Commands

### Admin commands
> Only available to user IDs listed in `ADMIN_IDS`.

| Command | Description |
|---|---|
| `/adduser <telegram_id> <marzban_username> [note]` | Register a user. Creates the Marzban account if it doesn't exist, then saves the mapping to the database and replies with the subscription URL. |
| `/removeuser <telegram_id>` | Shows a confirmation dialog before deleting the user from both Marzban and the database. |
| `/userinfo <telegram_id>` | Shows live stats: status, traffic used/limit, expiry date, and note. |
| `/listusers` | Paginated list of all registered users (10 per page) with Prev/Next buttons. |
| `/resettraffic <telegram_id>` | Resets the Marzban traffic counter for that user. |
| `/broadcast <message>` | Sends a message to every registered Telegram user (best-effort, failures are skipped). |

### User commands
> Available to any user registered in the database.

| Command | Description |
|---|---|
| `/start` | Welcome message with available commands. Unknown users receive a "no access" notice. |
| `/sub` | Fetches and displays the live subscription URL. |
| `/info` | Shows current account status, traffic usage, and expiry date. |

---

## Optional: run as a systemd service

Create `/etc/systemd/system/marzban-bot.service`:

```ini
[Unit]
Description=Marzban Telegram Bot
After=network.target

[Service]
Type=simple
User=youruser
WorkingDirectory=/opt/marzban-bot
EnvironmentFile=/opt/marzban-bot/.env
ExecStart=/usr/bin/python3 /opt/marzban-bot/bot.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Then enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now marzban-bot
sudo journalctl -u marzban-bot -f   # follow logs
```

---

## Notes

- New users are created in Marzban with **unlimited data** (`data_limit=0`) and **no expiry** (`expire=null`) using vless + vmess proxies. Adjust `marzban.py → create_user()` if your setup requires specific inbounds.
- Token refresh is handled automatically: if the Marzban API returns 401, the bot re-authenticates once and retries.
- All admin actions are logged to stdout with the acting admin's Telegram ID.
