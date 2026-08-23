"""U Network license/device monitoring service.

Polls licenses_get_licenses, tracks per-license online state in state.json,
prints console alerts and sends Telegram notifications when devices switch
between online and offline (with per-license ignore controls).
"""

import argparse
import json
import logging
import logging.handlers
import os
import signal
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from auth import AuthManager, AuthenticationError
from telegram import TelegramNotifier
from unetwork import ApiError, UNetworkClient

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("UNETWORK_CONFIG_FILE", BASE_DIR / "config.json"))
STATE_PATH = Path(os.environ.get("UNETWORK_STATE_FILE", BASE_DIR / "state.json"))

logger = logging.getLogger("monitor")

RECOVERY_CLOSED_TEXT = "🟢 Device Back Online\n\nDevice:\n{device_name}\n\nLicense:\n{license_short}\n\nOffline alert closed automatically."


class StopRun(Exception):
    pass


def _raise_stop(signum, frame):
    raise StopRun


def setup_logging():
    level = getattr(logging, os.environ.get("UNETWORK_LOG_LEVEL", "INFO").upper(), logging.INFO)
    
    # Create logs directory
    log_dir = BASE_DIR / "logs"
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / "unetwork-monitor.log"
    
    # Formatter matching existing terminal format
    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    
    # Console handler (existing behavior)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    console_handler.setLevel(level)
    
    # File handler with rotation (5 MB, keep 5 backups)
    file_handler = logging.handlers.RotatingFileHandler(
        log_file,
        maxBytes=5 * 1024 * 1024,  # 5 MB
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(level)
    
    # Configure root logger with both handlers
    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    root_logger.handlers = []  # Clear any existing handlers
    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)


def load_config():
    if not CONFIG_PATH.is_file():
        raise FileNotFoundError(
            f"Config file not found at {CONFIG_PATH}. Copy config.example.json to "
            "config.json and fill in your credentials."
        )
    with CONFIG_PATH.open(encoding="utf-8") as handle:
        return json.load(handle)


def load_state():
    if not STATE_PATH.is_file():
        return {"licenses": {}, "telegram_actions": {}}
    try:
        with STATE_PATH.open(encoding="utf-8") as handle:
            state = json.load(handle)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not read %s (%s); starting with empty state.", STATE_PATH, exc)
        return {"licenses": {}, "telegram_actions": {}}
    if isinstance(state, dict) and isinstance(state.get("licenses"), dict):
        state.setdefault("telegram_actions", {})
        return state
    if isinstance(state, dict) and isinstance(state.get("devices"), dict):
        logger.info("Legacy device-keyed state detected; starting a fresh license baseline.")
    else:
        logger.warning("Unrecognised state file structure; starting with empty state.")
    return {"licenses": {}, "telegram_actions": {}}


def save_state(state):
    payload = {
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "licenses": state.get("licenses", {}),
        "telegram_actions": state.get("telegram_actions", {}),
    }
    tmp_path = STATE_PATH.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp_path, STATE_PATH)


def get_or_create_action_id(state, license_id):
    """Map a license id to a short numeric Telegram callback id (persisted)."""
    actions = state.setdefault("telegram_actions", {})
    for action_id, mapping in actions.items():
        if isinstance(mapping, dict) and mapping.get("license_id") == license_id:
            return action_id
    next_id = str(max((int(k) for k in actions if str(k).isdigit()), default=0) + 1)
    actions[next_id] = {"license_id": license_id}
    return next_id


def short_id(device_id):
    """Render an identifier as <first 4>...<last 4> (e.g. 0x35e...5678)."""
    text = str(device_id)
    if len(text) <= 12:
        return text
    return f"{text[:4]}...{text[-4:]}"


def format_label(info):
    """'0x35e...5678 (Name)' or just the short license id when no name exists."""
    short = short_id(info.get("license_id") or "")
    name = info.get("device_name") or ""
    return f"{short} ({name})" if name else short


def build_message(header, info, footer=None):
    parts = [header, "", "Device:", info["device_name"]]
    if footer:
        parts += ["", footer]
    return "\n".join(parts)


def ignore_keyboard(action_id):
    """Inline keyboard; callback_data stays well under Telegram's 64-byte limit."""
    return {
        "inline_keyboard": [
            [{"text": "Ignore", "callback_data": f"ignore:{action_id}"}]
        ]
    }


def start_keyboard(action_id):
    """Keyboard for 'Start Monitoring' button (shown after Ignore)."""
    return {
        "inline_keyboard": [
            [{"text": "Start Monitoring", "callback_data": f"monitor:{action_id}"}]
        ]
    }


def snapshot_licenses(items):
    """Extract {license id: record}; licenses without a deviceName are ignored."""
    licenses = {}
    skipped = 0
    for item in items:
        license_id = item.get("id")
        if license_id is None:
            logger.warning("Skipping license entry without 'id': %s", str(item)[:120])
            continue
        device_name = (item.get("deviceName") or "").strip()
        if not device_name:
            skipped += 1
            continue
        licenses[str(license_id)] = {
            "license_id": str(license_id),
            "device_name": device_name,
            "status": "online" if item.get("isOnline") else "offline",
            "notification_state": "active",
        }
    if skipped:
        logger.info("Ignored %d license(s) without a bound device name.", skipped)
    return licenses


def _was_online(info):
    if isinstance(info, dict):
        status = info.get("status")
        if status:
            return status == "online"
        return bool(info.get("is_online"))
    return bool(info)


def detect_changes(previous, current):
    events = []
    for device_id, info in previous.items():
        was_online = _was_online(info)
        entry = current.get(device_id)
        if entry is None:
            label = (
                format_label(info)
                if isinstance(info, dict) and info.get("license_id")
                else short_id(device_id)
            )
            logger.warning(
                "Device %s no longer returned by the API; removing from tracked state.",
                label,
            )
            continue
        if was_online and entry["status"] == "offline":
            events.append((format_label(entry), "went offline", device_id))
        elif not was_online and entry["status"] == "online":
            events.append((format_label(entry), "came online", device_id))
        else:
            logger.debug("Device %s unchanged (%s).", format_label(entry), entry["status"])
    return events


def run_cycle(client, previous):
    licenses = list(client.iter_all_licenses())
    current = snapshot_licenses(licenses)
    online_count = sum(1 for info in current.values() if info["status"] == "online")
    logger.info("Fetched %d license(s); %d online.", len(current), online_count)

    if previous:
        for device_id, info in current.items():
            if device_id not in previous:
                logger.info(
                    "New device discovered: %s is %s.",
                    format_label(info),
                    info["status"],
                )
        events = detect_changes(previous, current)
    else:
        events = []
        logger.info("First run: baseline saved with %d device(s); no alerts generated.", len(current))

    return current, events


def _parse_ts(value):
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _minutes_between(start, end):
    if start is None:
        return None
    return max(int((end - start).total_seconds() // 60), 0)


def make_action_handler(holder, state_lock, notifier):
    """Build the Telegram button callback handler (ignore / start monitoring).

    Callbacks carry a short action id resolved to the full license id via the
    telegram_actions mapping in state.json. Pressed messages are edited in
    place so buttons can never be used twice.

    Returns (toast_text, followup_jobs) for the callback poller.
    """

    def handle_action(action, action_id, message_id=None):
        if action == "start":
            action = "monitor"

        with state_lock:
            mapping = holder.get("telegram_actions", {}).get(str(action_id))
            if not isinstance(mapping, dict) or "license_id" not in mapping:
                logger.warning("Telegram action for unknown short id %r; ignoring.", action_id)
                return "Unknown alert.", []
            license_id = mapping["license_id"]
            entry = holder["licenses"].get(license_id)
            if entry is None:
                logger.warning(
                    "Telegram action targets license %s which is no longer tracked; ignoring.",
                    short_id(license_id),
                )
                return "This license is no longer tracked.", []

            device_name = entry.get("device_name") or "Unknown Device"
            alert_state = entry.get("notification_state")
            # Migrate legacy 'ignored' field
            if alert_state is None:
                if entry.get("ignored"):
                    alert_state = "ignored"
                else:
                    alert_state = "active"
            elif alert_state == "unhandled":
                alert_state = "active"

            # Handle recovery - if device is online and alert was recovered, alert is closed
            # But allow "monitor" action to resume monitoring
            if entry.get("status") == "online" and alert_state == "recovered" and action not in ("monitor", "start"):
                return f"Alert already closed: {device_name} is online.", []

            followup_jobs = []

            if action == "ignore":
                if alert_state == "ignored":
                    return "Already ignored.", []

                entry["notification_state"] = "ignored"
                save_state(holder)
                logger.info(
                    "License %s (%s) ignored via Telegram; reminders stopped.",
                    short_id(license_id),
                    device_name,
                )
                if message_id and notifier:
                    # Edit message to show ignored state with Start Monitoring button
                    def edit_ignored():
                        notifier.edit_message(
                            build_message("✅ Ignored", entry, "The alert is now ignored."),
                            message_id,
                            buttons=start_keyboard(action_id),
                        )
                    followup_jobs.append(edit_ignored)
                return "Ignored. Reminders stopped.", followup_jobs

            if action in ("monitor", "start"):
                if alert_state == "monitoring":
                    return "Monitoring already started.", []
                if alert_state != "ignored":
                    return "Please ignore the alert first before starting monitoring.", []

                # Check current device status
                current_status = entry.get("status", "unknown")
                
                # If device is still offline, send a new offline notification immediately
                if current_status == "offline":
                    entry["notification_state"] = "active"
                    # Keep the original offline_since timestamp
                    # Resume reminders from the original offline time
                    if not entry.get("offline_since"):
                        entry["offline_since"] = datetime.now(timezone.utc).isoformat()
                    entry["last_reminder_sent"] = entry["offline_since"]
                    save_state(holder)
                    logger.info(
                        "Monitoring resumed for license %s (%s) via Telegram; device still offline, sending alert.",
                        short_id(license_id),
                        device_name,
                    )
                    if message_id and notifier:
                        def edit_monitoring_started():
                            notifier.edit_message(
                                build_message("🔵 Monitoring Started", entry, "Monitoring has started."),
                                message_id,
                                buttons=None,
                            )
                        followup_jobs.append(edit_monitoring_started)
                    # Send new offline notification
                    outgoing_jobs = []
                    def send_offline_alert():
                        action_id = get_or_create_action_id(holder, license_id)
                        msg_id = notifier.notify(
                            build_message("🔴 Device Offline", entry, f"License:\n{short_id(license_id)}"),
                            buttons=ignore_keyboard(action_id),
                        )
                        if msg_id:
                            with state_lock:
                                e = holder["licenses"].get(license_id)
                                if e is not None:
                                    e["telegram_message_id"] = msg_id
                                    save_state(holder)
                    outgoing_jobs.append(send_offline_alert)
                    return "Monitoring resumed. Device still offline — alert sent.", followup_jobs + outgoing_jobs
                else:
                    # Device is online, just resume monitoring
                    entry["notification_state"] = "active"
                    save_state(holder)
                    logger.info(
                        "Monitoring started for license %s (%s) via Telegram; device is online.",
                        short_id(license_id),
                        device_name,
                    )
                    if message_id and notifier:
                        def edit_monitoring_started():
                            notifier.edit_message(
                                build_message("🔵 Monitoring Started", entry, "Monitoring has started."),
                                message_id,
                                buttons=None,
                            )
                        followup_jobs.append(edit_monitoring_started)
                    return "Monitoring started. Device is online.", followup_jobs

            return "Unknown action.", []

            return "Unknown action.", []

    return handle_action


def run_loop(client, config, once=False, notifier=None):
    interval = max(int(config.get("poll_interval_seconds", 60)), 1)
    reminder_seconds = int(config.get("reminder_interval_minutes", 5)) * 60
    state_lock = threading.Lock()
    holder = load_state()

    if notifier:
        notifier.start_callback_poller(make_action_handler(holder, state_lock, notifier))

    while True:
        started = time.monotonic()
        try:
            with state_lock:
                frozen_previous = {k: dict(v) for k, v in holder["licenses"].items()}

            current, events = run_cycle(client, frozen_previous)
            now_dt = datetime.now(timezone.utc)
            outgoing = []

            with state_lock:
                live_previous = holder["licenses"]

                for license_id, info in current.items():
                    prev = live_previous.get(license_id)
                    if not isinstance(prev, dict):
                        continue
                    prev_state = prev.get("notification_state")
                    if prev_state == "unhandled":
                        prev_state = "active"
                    if prev_state:
                        info["notification_state"] = prev_state
                    elif prev.get("ignored"):
                        info["notification_state"] = "ignored"
                    if prev.get("telegram_message_id"):
                        info["telegram_message_id"] = prev["telegram_message_id"]
                    if info["status"] != "offline":
                        continue
                    prev_since = prev.get("offline_since")
                    if prev_since and _parse_ts(prev_since):
                        info["offline_since"] = prev_since
                        info["last_reminder_sent"] = (
                            prev.get("last_reminder_sent") or prev.get("last_notification") or prev_since
                        )

                for label, event, license_id in events:
                    info = current[license_id]
                    print(f"Device {label} {event}", flush=True)
                    if event == "went offline":
                        alert_state = info.get("notification_state") or "active"
                        if alert_state == "unhandled":
                            alert_state = "active"
                        if alert_state in ("ignored", "monitoring"):
                            logger.info(
                                "License %s (%s) went offline but is %s; no notification.",
                                short_id(license_id),
                                info["device_name"],
                                alert_state,
                            )
                            continue
                        info["notification_state"] = "active"
                        info["offline_since"] = now_dt.isoformat()
                        info["last_reminder_sent"] = info["offline_since"]
                        if notifier:
                            outgoing.append({
                                "kind": "send",
                                "license_id": license_id,
                                "text": build_message(
                                    "🔴 Device Offline", info, f"License:\n{short_id(license_id)}"
                                ),
                                "buttons": ignore_keyboard(get_or_create_action_id(holder, license_id)),
                                "track": True,
                            })
                    else:
                        since_raw = None
                        stale_message_id = None
                        stale_device_name = None
                        for source in (live_previous.get(license_id), frozen_previous.get(license_id)):
                            if isinstance(source, dict):
                                if source.get("offline_since") and since_raw is None:
                                    since_raw = source["offline_since"]
                                if source.get("telegram_message_id"):
                                    stale_message_id = source["telegram_message_id"]
                                if source.get("device_name"):
                                    stale_device_name = source["device_name"]
                        minutes = _minutes_between(_parse_ts(since_raw), now_dt)
                        license_short = short_id(license_id)
                        footer = f"License:\n{license_short}\n\nOffline duration:\n{minutes} minutes" if minutes is not None else f"License:\n{license_short}"
                        if notifier:
                            outgoing.append({
                                "kind": "send",
                                "license_id": license_id,
                                "text": build_message("🟢 Device Online", info, footer),
                                "buttons": None,
                                "track": False,
                            })
                            if stale_message_id:
                                outgoing.append({
                                    "kind": "edit",
                                    "message_id": stale_message_id,
                                    "text": RECOVERY_CLOSED_TEXT.format(device_name=stale_device_name or info["device_name"], license_short=license_short),
                                })
                        info["notification_state"] = "recovered"
                        if minutes is not None:
                            logger.info(
                                "License %s back online after %d minute(s) offline.",
                                short_id(license_id),
                                minutes,
                            )

                for license_id, info in current.items():
                    if info["status"] != "offline" or info.get("notification_state") not in (
                        "active",
                    ):
                        continue
                    offline_ts = _parse_ts(info.get("offline_since"))
                    if not offline_ts:
                        logger.info(
                            "License %s offline without recorded start time; starting clock now.",
                            format_label(info),
                        )
                        info["offline_since"] = now_dt.isoformat()
                        info["last_reminder_sent"] = info["offline_since"]
                        continue
                    last_sent = _parse_ts(info.get("last_reminder_sent")) or offline_ts
                    if (now_dt - last_sent).total_seconds() >= reminder_seconds:
                        boundaries_passed = int((now_dt - offline_ts).total_seconds() // reminder_seconds)
                        scheduled = offline_ts + timedelta(seconds=boundaries_passed * reminder_seconds)
                        info["last_reminder_sent"] = scheduled.isoformat()
                        minutes = boundaries_passed * (reminder_seconds // 60)
                        if notifier:
                            outgoing.append({
                                "kind": "send",
                                "license_id": license_id,
                                "text": build_message(
                                    "⚠️ Device Offline Reminder", info, f"Offline for:\n{minutes} minutes"
                                ),
                                "buttons": ignore_keyboard(get_or_create_action_id(holder, license_id)),
                                "track": True,
                            })
                        logger.info(
                            "Offline reminder sent for license %s (%d minute(s) offline).",
                            short_id(license_id),
                            minutes,
                        )

                holder["licenses"] = current
                save_state(holder)

            if notifier:
                for item in outgoing:
                    if item["kind"] == "edit":
                        notifier.edit_message(item["text"], item["message_id"])
                        continue
                    message_id = notifier.notify(item["text"], buttons=item["buttons"])
                    if item["track"] and message_id:
                        with state_lock:
                            entry = holder["licenses"].get(item["license_id"])
                            if entry is not None:
                                entry["telegram_message_id"] = message_id
                                save_state(holder)
        except AuthenticationError as exc:
            logger.error("Authentication requires manual attention: %s", exc)
            return 1
        except (ApiError, ConnectionError, requests.RequestException) as exc:
            logger.error("Poll cycle failed (will retry next cycle): %s", exc)
        except Exception:
            logger.exception("Unexpected error during poll cycle (will retry).")

        if once:
            break
        time.sleep(max(0.0, interval - (time.monotonic() - started)))

    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Monitor U Network device online status.")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single poll cycle and exit (useful for testing or scheduled tasks).",
    )
    args = parser.parse_args(argv)

    setup_logging()
    signal.signal(signal.SIGTERM, _raise_stop)

    try:
        config = load_config()
        auth = AuthManager(config, CONFIG_PATH)
        client = UNetworkClient(auth, config)
    except (AuthenticationError, FileNotFoundError, json.JSONDecodeError) as exc:
        logger.error("Startup failed: %s", exc)
        return 2

    try:
        auth.get_access_token()
        logger.info("Credentials verified; monitoring started (state file: %s).", STATE_PATH)
    except AuthenticationError as exc:
        logger.error("Startup authentication failed: %s", exc)
        return 2

    try:
        return run_loop(client, config, once=args.once, notifier=TelegramNotifier(config))
    except (KeyboardInterrupt, StopRun):
        logger.info("Shutdown requested; exiting.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
