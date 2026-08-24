# Unetwork Device Monitor

A Python service that monitors Unetwork device online/offline status and sends Telegram notifications.

## Features

- Polls Unetwork API every 60 seconds for device license status
- Detects online/offline state changes
- Sends Telegram notifications for:
  - Device goes offline
  - Device comes back online (with offline duration)
  - Periodic reminders while device is offline (every 5 minutes)
  - Authentication failures (refresh token expired/rejected)
- Interactive Telegram buttons:
  - **Ignore** - Stop reminders for a device
  - **Start Monitoring** - Resume monitoring after ignoring
- Persistent state tracking (survives restarts)
- Rotating log files (5 MB, 5 backups)
- Robust authentication with automatic token refresh

## Prerequisites

- Python 3.10+
- Unetwork account with access to the API
- Telegram account

## Telegram Bot Setup

### 1. Create a Telegram Bot

1. Open Telegram and search for **@BotFather**
2. Send `/newbot` command
3. Follow the prompts:
   - Enter a name for your bot (e.g., "Unetwork Monitor")
   - Enter a username ending in `bot` (e.g., `unetwork_monitor_bot`)
3. BotFather will provide a **Bot Token** (format: `123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ`)

### 2. Get Your Chat ID

1. Send a message to your newly created bot (any message like `/start`)
2. In a browser, visit:
   ```
   https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates
   ```
   Replace `<YOUR_BOT_TOKEN>` with your actual bot token
3. Look for the `"chat":{"id":...}` field in the JSON response
   - For private chats: positive number (e.g., `123456789`)
   - For groups: negative number (e.g., `-1001234567890`)

### 3. Alternative: Use @userinfobot

1. Search for **@userinfobot** in Telegram
2. Send any message
3. It will reply with your user ID (this is your chat ID for private messages)

## Configuration

### 1. Create config.json

Copy the example configuration:
```bash
cp config.example.json config.json
```

### 2. Edit config.json

```json
{
  "api_base_url": "https://api.unityedge.io",
  "supabase_publishable_key": "your_supabase_publishable_key_here",
  "refresh_token": "your_refresh_token_here",
  "telegram_bot_token": "your_telegram_bot_token_here",
  "telegram_chat_id": "your_chat_id_here",
  "telegram_enabled": true,
  "role": "ulo",
  "page_size": 20,
  "enabled": true,
  "poll_interval_seconds": 60,
  "refresh_margin_seconds": 120,
  "request_timeout_seconds": 30,
  "http_max_attempts": 3,
  "max_pages_per_poll": 100,
  "reminder_interval_minutes": 5
}
```

### Required Fields

| Field | Description |
|-------|-------------|
| `supabase_publishable_key` | Your Supabase publishable API key from Unetwork |
| `refresh_token` | Your Unetwork refresh token (obtained from manual login) |
| `telegram_bot_token` | Bot token from @BotFather |
| `telegram_chat_id` | Your chat ID from getUpdates or @userinfobot |


## Getting Your Unetwork Credentials

### Supabase Token

Google it or take help from LLMs

### Refresh Token

1. Log into Unetwork dashboard manually (web browser)
2. Open browser DevTools → Application → Local Storage
3. Find the `refresh_token` value
4. Copy the token value

## Installation

### 1. Clone the repository

```bash
git clone <repository-url>
cd unetwork-monitor
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Create and configure `config.json`

Copy the example configuration:

```bash
cp config.example.json config.json
```

Then edit `config.json`:

```bash
nano config.json
```

Make the required changes for your Unetwork and Telegram credentials.

At minimum, configure:

- `supabase_publishable_key`
- `refresh_token`
- `telegram_bot_token`
- `telegram_chat_id`
- `telegram_enabled`

Do **not** commit or share `config.json`.

## Running the Monitor

### Foreground (Development/Testing)

Run one polling cycle and exit:

```bash
python main.py --once
```

Run continuously in the foreground:

```bash
python main.py
```

### (BETTER TO USE LLMs) Background/Production (systemd) - 

Enable and start the service:

```bash
sudo systemctl daemon-reload
sudo systemctl enable unetwork-monitor
sudo systemctl start unetwork-monitor
```

Check the service:

```bash
sudo systemctl status unetwork-monitor --no-pager
```

Expected:

```text
Active: active (running)
```

Quick check:

```bash
sudo systemctl is-active unetwork-monitor
```

Expected:

```text
active
```

Stop the service:

```bash
sudo systemctl stop unetwork-monitor
```

Verify:

```bash
sudo systemctl status unetwork-monitor --no-pager
```

Expected:

```text
Active: inactive (dead)
```

Restart after configuration changes:

```bash
sudo systemctl restart unetwork-monitor
```

### Logs

If the application is configured to write to `logs/unetwork-monitor.log`, view the previous 100 lines:

```bash
tail -100 logs/unetwork-monitor.log
```

Follow logs live:

```bash
tail -f logs/unetwork-monitor.log
```

Press `Ctrl + C` to stop watching the logs. This does not stop the monitor service.

For systemd logs:

```bash
sudo journalctl -u unetwork-monitor -f
```

## Telegram Interaction

### Device Offline Alert
```
🔴 Device Offline

Device:
Galaxy M21

License:
0x35e...d8de

[Ignore]
```

### After Clicking Ignore
```
✅ Ignored

Device:
Galaxy M21

The alert is now ignored.

[Start Monitoring]
```

### After Clicking Start Monitoring
```
🔵 Monitoring Started

Device:
Galaxy M21

Monitoring has started.
```

### Device Comes Online
```
🟢 Device Online

Device:
Galaxy M21

License:
0x35e...d8de

Offline duration:
15 minutes
```

### Automatic Recovery (Device comes online without user action)
The old offline message is updated to:
```
🟢 Device Back Online

Device:
Galaxy M21

License:
0x35e...d8de

Offline alert closed automatically.
```

### Authentication Failure
```
🔴 Unetwork Authentication Failed

The refresh token has expired or was rejected.

The monitor cannot refresh its access token and requires manual attention.

Please manually update/fix the refresh token and restart the monitor.
```

## State Management

The monitor maintains state in `state.json`:

```json
{
  "licenses": {
    "license_id": {
      "license_id": "...",
      "device_name": "Galaxy M21",
      "status": "offline",
      "notification_state": "active",
      "offline_since": "2026-08-23T14:00:00+00:00",
      "last_reminder_sent": "2026-08-23T14:05:00+00:00",
      "telegram_message_id": 42
    }
  },
  "telegram_actions": {
    "1": { "license_id": "0x35e..." }
  },
  "auth_failure_notified": false
}
```

## Log Files

Logs are written to:
- Console (stdout)
- Rotating log files in `logs/unetwork-monitor.log`
  - Max size: 5 MB
  - Backup count: 5 files
  - Format: `2026-08-23 19:27:20 INFO    monitor | Credentials verified...`

## Troubleshooting

### "Credentials verified" but no notifications
- Check `telegram_enabled` is `true` in config.json
- Verify bot token and chat ID are correct
- Send a message to the bot first (required for private chats)
- Check logs for Telegram API errors

### "Authentication requires manual attention"
- Refresh token has expired or been revoked
- Log into Unetwork dashboard manually to get new refresh token
- Update `refresh_token` in config.json
- Restart the monitor

### "Refresh token rejected"
- Token may have been used elsewhere (only one active session)
- Get new refresh token from manual login
- Update config.json and restart

### No devices found
- Ensure `deviceName` is set in Unetwork dashboard for your devices
- Devices without a name are ignored

## File Structure

```
unetwork-monitor/
├── main.py              # Main monitoring loop
├── auth.py              # Authentication & token refresh
├── unetwork.py          # Unetwork API client
├── telegram.py          # Telegram bot notifications
├── config.json          # Configuration (create from example)
├── config.example.json  # Example configuration
├── state.json           # Persistent state (auto-generated)
├── requirements.txt     # Python dependencies
├── logs/
│   └── unetwork-monitor.log  # Rotating log files
└── README.md            # This file
```

## Security Notes

- Never commit `config.json` or `state.json` to version control
- Use environment variables for sensitive values in production
- The `.gitignore` excludes sensitive files by default
- Bot token and chat ID are not logged
