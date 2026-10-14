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
USERS_PATH = Path(os.environ.get("UNETWORK_USERS_FILE", BASE_DIR / "users.json"))

logger = logging.getLogger("monitor")

RECOVERY_CLOSED_TEXT = "🟢 Device Back Online\n\nDevice:\n{device_name}\n\nLicense:\n{license_short}\n\nOffline alert closed automatically."

SUBSCRIBE_PAGE_SIZE = 50


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
        return {"licenses": {}, "telegram_actions": {}, "auth_failure_notified": False}
    try:
        with STATE_PATH.open(encoding="utf-8") as handle:
            state = json.load(handle)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not read %s (%s); starting with empty state.", STATE_PATH, exc)
        return {"licenses": {}, "telegram_actions": {}, "auth_failure_notified": False}
    if isinstance(state, dict) and isinstance(state.get("licenses"), dict):
        state.setdefault("telegram_actions", {})
        state.setdefault("auth_failure_notified", False)
        return state
    if isinstance(state, dict) and isinstance(state.get("devices"), dict):
        logger.info("Legacy device-keyed state detected; starting a fresh license baseline.")
    else:
        logger.warning("Unrecognised state file structure; starting with empty state.")
    return {"licenses": {}, "telegram_actions": {}, "auth_failure_notified": False}


def save_state(state):
    payload = {
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "licenses": state.get("licenses", {}),
        "telegram_actions": state.get("telegram_actions", {}),
        "auth_failure_notified": state.get("auth_failure_notified", False),
    }
    tmp_path = STATE_PATH.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp_path, STATE_PATH)


def empty_users():
    return {"users": {}}


def load_users():
    """Approved-user registry (gitignored). Missing/broken file => no users."""
    if not USERS_PATH.is_file():
        return empty_users()
    try:
        with USERS_PATH.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning(
            "Could not read %s (%s); starting with no approved users.", USERS_PATH, exc
        )
        return empty_users()
    if isinstance(data, dict) and isinstance(data.get("users"), dict):
        return {"users": data["users"]}
    logger.warning("Unrecognised users file structure; starting with no approved users.")
    return empty_users()


def save_users(data):
    payload = {
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "users": data.get("users", {}),
    }
    tmp_path = USERS_PATH.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp_path, USERS_PATH)


def admin_chat_id(config):
    """The chat id configured for the primary (admin) bot."""
    return str(config.get("telegram_chat_id") or "").strip()


def is_admin_sender(sender, config):
    """True when the update came from the configured admin chat/user."""
    admin = admin_chat_id(config)
    if not admin or not isinstance(sender, dict):
        return False
    return (
        str(sender.get("from_id") or "") == admin
        or str(sender.get("chat_id") or "") == admin
    )


def approved_user(users_data, ident):
    """Approved registry entry for a user/chat id, else None."""
    if not ident:
        return None
    entry = (users_data.get("users") or {}).get(str(ident))
    if isinstance(entry, dict) and entry.get("approved"):
        return entry
    return None


def resolve_user(users_data, sender):
    """Approved registry entry for an incoming update's sender."""
    if not isinstance(sender, dict):
        return None
    for key in ("from_id", "chat_id"):
        entry = approved_user(users_data, sender.get(key))
        if entry is not None:
            return entry
    return None


def user_alert_state(info, user_id):
    """Per-user alert state for a license (defaults to 'active')."""
    record = (info.get("users") or {}).get(str(user_id))
    if not isinstance(record, dict):
        return "active"
    return record.get("alert_state") or "active"


def set_user_alert_state(info, user_id, state):
    """Write the per-user alert state for a license; returns the record."""
    records = info.get("users")
    if not isinstance(records, dict):
        records = {}
        info["users"] = records
    record = records.setdefault(str(user_id), {"alert_state": "active"})
    record["alert_state"] = state
    return record


def user_subscription_ids(user_entry):
    return [str(item) for item in (user_entry.get("subscriptions") or [])]


def toggle_subscription(user_entry, license_id):
    """Add/remove a license from a user's follow list; True = now following."""
    subs = user_subscription_ids(user_entry)
    license_id = str(license_id)
    if license_id in subs:
        subs.remove(license_id)
        following = False
    else:
        subs.append(license_id)
        following = True
    user_entry["subscriptions"] = subs
    return following


def user_chat_id(user_entry):
    """Where a user's copies of alerts should be delivered."""
    return str(user_entry.get("chat_id") or user_entry.get("user_id") or "")


def subscribed_users(users_data, info, want=None):
    """[(chat_id, user_id)] for approved users following this license.

    `want` optionally filters on the per-user alert state.
    """
    license_id = str(info.get("license_id") or "")
    targets = []
    for user_id, entry in (users_data.get("users") or {}).items():
        if not isinstance(entry, dict) or not entry.get("approved"):
            continue
        if license_id not in user_subscription_ids(entry):
            continue
        if want is not None and not want(user_alert_state(info, user_id)):
            continue
        chat_id = user_chat_id(entry)
        if not chat_id:
            continue
        targets.append((chat_id, str(user_id)))
    return targets


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


def license_toggle_keyboard(action_id, enabled):
    """Keyboard for license toggle in /licenses list."""
    text = "🟢 Active" if enabled else "🔴 Inactive"
    return {
        "inline_keyboard": [
            [{"text": text, "callback_data": f"toggle:{action_id}"}]
        ]
    }


def build_licenses_list(holder):
    """Build /licenses list text plus one toggle button per license row."""
    licenses = holder.get("licenses", {})
    if not licenses:
        return "📋 Monitored Licenses\n\nNo licenses tracked yet.", None
    lines = ["📋 Monitored Licenses", "", "Tap a license to turn monitoring on/off:"]
    keyboard = []
    for license_id in sorted(licenses):
        info = licenses[license_id]
        action_id = get_or_create_action_id(holder, license_id)
        enabled = info.get("monitoring_enabled", True)
        name = (info.get("device_name") or "?")[:24]
        lines.append(f"{'🟢' if enabled else '🔴'} {short_id(license_id)} | {name}")
        keyboard.append([{
            "text": f"{short_id(license_id)} | {name} | {'Active' if enabled else 'Inactive'}",
            "callback_data": f"toggle:{action_id}",
        }])
    return "\n".join(lines), {"inline_keyboard": keyboard}


def build_subscribe_list(holder, users_data, user_id, page=1):
    """Build the /addDevices list for one subscriber.

    Returns (text, keyboard, page, total_pages). Each row toggles a single
    license for this user via the `sub` callback; a nav row pages the list so
    we stay under Telegram's 100-button / 4096-character limits.
    """
    licenses = holder.get("licenses", {})
    if not licenses:
        return "📱 My Devices\n\nNo licenses tracked yet.", None, 1, 1
    user_entry = (users_data.get("users") or {}).get(str(user_id)) or {}
    following = set(user_subscription_ids(user_entry))
    license_ids = sorted(licenses)
    total_pages = max(1, (len(license_ids) + SUBSCRIBE_PAGE_SIZE - 1) // SUBSCRIBE_PAGE_SIZE)
    page = min(max(int(page or 1), 1), total_pages)
    start = (page - 1) * SUBSCRIBE_PAGE_SIZE

    lines = [
        "📱 My Devices",
        "",
        "Tap a device to follow or stop its updates:",
        f"(page {page}/{total_pages})",
        "",
    ]
    keyboard = []
    for license_id in license_ids[start:start + SUBSCRIBE_PAGE_SIZE]:
        info = licenses.get(license_id) or {}
        action_id = get_or_create_action_id(holder, license_id)
        is_following = license_id in following
        name = (info.get("device_name") or "?")[:24]
        mark = "🟢" if is_following else "⚪"
        lines.append(f"{mark} {short_id(license_id)} | {name}")
        keyboard.append([
            {
                "text": f"{mark} {short_id(license_id)} | {name}",
                "callback_data": f"sub:{action_id}:{page}",
            }
        ])

    nav = []
    if page > 1:
        nav.append({"text": "⬅️ Prev", "callback_data": f"page:{page - 1}"})
    if page < total_pages:
        nav.append({"text": "Next ➡️", "callback_data": f"page:{page + 1}"})
    if nav:
        keyboard.append(nav)
    return "\n".join(lines), {"inline_keyboard": keyboard}, page, total_pages


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
            "monitoring_enabled": True,
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


def make_action_handler(holder, users_holder, state_lock, notifier, config):
    """Build the Telegram button callback handler.

    Callbacks carry a short action id resolved to the full license id via the
    telegram_actions mapping in state.json. Pressed messages are edited in
    place so buttons can never be used twice.

    Admin senders drive the original per-license controls (ignore / start
    monitoring / global toggle). Approved subscribers instead drive their own
    alert state (`ignore` / `monitor`) plus the /addDevices follow list
    (`sub`) and its paging (`page`).

    Returns (toast_text, followup_jobs) for the callback poller.
    """

    def handle_action(action, action_id, message_id=None, sender=None):
        sender = sender if isinstance(sender, dict) else {}
        if action == "start":
            action = "monitor"

        is_admin = is_admin_sender(sender, config)
        from_id = str(sender.get("from_id") or "")
        chat_id = str(sender.get("chat_id") or "")
        user_entry = None
        if not is_admin:
            user_entry = resolve_user(users_holder, sender)
            if user_entry is None:
                logger.warning(
                    "Button press from unapproved sender %s ignored.",
                    from_id or chat_id or "?",
                )
                return "You are not approved.", []
        user_id = (
            str(user_entry.get("user_id") or from_id or chat_id) if user_entry else ""
        )

        # Pure navigation: re-render the subscriber list for the requested page.
        if action == "page":
            if is_admin:
                return "Admins already see every device.", []
            page = int(action_id) if str(action_id).isdigit() else 1

            def refresh_page():
                with state_lock:
                    text, keyboard, _, _ = build_subscribe_list(
                        holder, users_holder, user_id, page
                    )
                    save_state(holder)
                if notifier and chat_id:
                    notifier.edit_message(
                        text, message_id, buttons=keyboard, chat_id=chat_id
                    )

            return None, [refresh_page]

        # `sub` callbacks carry "<action_id>:<page>" so the list can re-render.
        mapping_ref = str(action_id)
        page = 1
        if action == "sub":
            mapping_ref, _, page_raw = mapping_ref.partition(":")
            page = int(page_raw) if page_raw.isdigit() else 1

        with state_lock:
            mapping = holder.get("telegram_actions", {}).get(str(mapping_ref))
            if not isinstance(mapping, dict) or "license_id" not in mapping:
                logger.warning(
                    "Telegram action for unknown short id %r; ignoring.", mapping_ref
                )
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
            followup_jobs = []

            # ---- /addDevices follow toggle (subscribers only) ----
            if action == "sub":
                if user_entry is None:
                    return "Admins already see every device.", []
                following = toggle_subscription(user_entry, license_id)
                save_users(users_holder)
                logger.info(
                    "License %s (%s) %s via /addDevices by user %s.",
                    short_id(license_id),
                    device_name,
                    "followed" if following else "unfollowed",
                    user_id,
                )
                if message_id and notifier and chat_id:

                    def refresh_subs():
                        with state_lock:
                            text, keyboard, _, _ = build_subscribe_list(
                                holder, users_holder, user_id, page
                            )
                            save_state(holder)
                        notifier.edit_message(
                            text, message_id, buttons=keyboard, chat_id=chat_id
                        )

                    followup_jobs.append(refresh_subs)
                if following:
                    return f"✅ {device_name}: you will now receive its updates.", followup_jobs
                return f"⚪ {device_name}: you will stop receiving its updates.", followup_jobs

            # ---- Per-user alert controls (non-admin senders) ----
            if not is_admin:
                if action == "toggle":
                    return "Only the admin can change global monitoring.", []
                records = entry.get("users")
                if not isinstance(records, dict):
                    records = {}
                    entry["users"] = records
                user_state = records.get(user_id)
                if not isinstance(user_state, dict):
                    user_state = {"alert_state": "active"}
                    records[user_id] = user_state
                user_alert = user_state.get("alert_state") or "active"

                if (
                    entry.get("status") == "online"
                    and user_alert == "recovered"
                    and action != "monitor"
                ):
                    return f"Alert already closed: {device_name} is online.", []

                if action == "ignore":
                    if user_alert == "ignored":
                        return "Already ignored.", []
                    user_state["alert_state"] = "ignored"
                    save_state(holder)
                    logger.info(
                        "License %s (%s) ignored by user %s; their reminders stopped.",
                        short_id(license_id),
                        device_name,
                        user_id,
                    )
                    if message_id and notifier and chat_id:

                        def edit_ignored_user():
                            notifier.edit_message(
                                build_message(
                                    "✅ Ignored", entry, "The alert is now ignored."
                                ),
                                message_id,
                                buttons=start_keyboard(action_id),
                                chat_id=chat_id,
                            )

                        followup_jobs.append(edit_ignored_user)
                    return "Ignored. Reminders stopped.", followup_jobs

                if action == "monitor":
                    if user_alert == "monitoring":
                        return "Monitoring already started.", []
                    if user_alert != "ignored":
                        return "Please ignore the alert first before starting monitoring.", []
                    user_state["alert_state"] = "active"
                    save_state(holder)
                    logger.info(
                        "Monitoring resumed for license %s (%s) by user %s.",
                        short_id(license_id),
                        device_name,
                        user_id,
                    )
                    if message_id and notifier and chat_id:

                        def edit_monitoring_user():
                            notifier.edit_message(
                                build_message(
                                    "🔵 Monitoring Started",
                                    entry,
                                    f"License:\n{short_id(license_id)}\n\nMonitoring has started.",
                                ),
                                message_id,
                                buttons=None,
                                chat_id=chat_id,
                            )

                        followup_jobs.append(edit_monitoring_user)
                    if entry.get("status") == "offline" and notifier and chat_id:

                        def send_user_offline_alert():
                            alert_action_id = get_or_create_action_id(holder, license_id)
                            msg_id = notifier.notify(
                                build_message(
                                    "🔴 Device Offline",
                                    entry,
                                    f"License:\n{short_id(license_id)}",
                                ),
                                buttons=ignore_keyboard(alert_action_id),
                                chat_id=chat_id,
                            )
                            if msg_id:
                                with state_lock:
                                    live = holder["licenses"].get(license_id)
                                    if live is not None:
                                        set_user_alert_state(live, user_id, "active")[
                                            "message_id"
                                        ] = msg_id
                                        save_state(holder)

                        followup_jobs.append(send_user_offline_alert)
                        return (
                            "Monitoring started. Device still offline — alert sent.",
                            followup_jobs,
                        )
                    return "Monitoring started.", followup_jobs

                return "Unknown action.", []

            # ---- Admin controls (unchanged behaviour) ----
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
            # But allow "monitor" action to resume monitoring and "toggle" (master switch)
            if entry.get("status") == "online" and alert_state == "recovered" and action not in ("monitor", "start", "toggle"):
                return f"Alert already closed: {device_name} is online.", []

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
                        "Monitoring started for license %s (%s) via Telegram; device still offline, sending alert.",
                        short_id(license_id),
                        device_name,
                    )
                    if message_id and notifier:
                        def edit_monitoring_started():
                            notifier.edit_message(
                                build_message("🔵 Monitoring Started", entry, f"License:\n{short_id(license_id)}\n\nMonitoring has started."),
                                message_id,
                                buttons=None,
                            )
                        followup_jobs.append(edit_monitoring_started)
                    # Send new offline notification
                    outgoing_jobs = []

                    def send_offline_alert():
                        alert_action_id = get_or_create_action_id(holder, license_id)
                        msg_id = notifier.notify(
                            build_message("🔴 Device Offline", entry, f"License:\n{short_id(license_id)}"),
                            buttons=ignore_keyboard(alert_action_id),
                        )
                        if msg_id:
                            with state_lock:
                                e = holder["licenses"].get(license_id)
                                if e is not None:
                                    e["telegram_message_id"] = msg_id
                                    save_state(holder)

                    if notifier:
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
                                build_message("🔵 Monitoring Started", entry, f"License:\n{short_id(license_id)}\n\nMonitoring has started."),
                                message_id,
                                buttons=None,
                            )
                        followup_jobs.append(edit_monitoring_started)
                    return "Monitoring started. Device is online.", followup_jobs

            if action == "toggle":
                enabled = entry.get("monitoring_enabled", True)
                entry["monitoring_enabled"] = not enabled
                save_state(holder)
                logger.info(
                    "License %s (%s) monitoring %s via /licenses.",
                    short_id(license_id),
                    device_name,
                    "enabled" if not enabled else "disabled",
                )
                if message_id and notifier:
                    def refresh_licenses_list():
                        fresh_text, fresh_keyboard = build_licenses_list(holder)
                        notifier.edit_message(fresh_text, message_id, buttons=fresh_keyboard)
                    followup_jobs.append(refresh_licenses_list)
                if enabled:
                    return f"Monitoring disabled for {device_name}.", followup_jobs
                return f"Monitoring enabled for {device_name}.", followup_jobs

            return "Unknown action.", []

    return handle_action


def make_command_handler(holder, users_holder, state_lock, notifier, config):
    """Build the bot command handler.

    Admin (the configured telegram_chat_id) gets /licenses plus the user
    management commands. Anyone else only gets /start and, once approved,
    /addDevices. Replies go back to the sender's own chat, except /licenses
    which keeps its original behaviour of posting into the configured chat.
    """

    def reply(text, sender, buttons=None):
        if not notifier:
            return
        chat_id = (sender or {}).get("chat_id")
        notifier.notify(text, buttons=buttons, chat_id=chat_id or None)

    def handle_command(text, sender=None):
        sender = sender if isinstance(sender, dict) else {}
        parts = (text or "").strip().split()
        if not parts:
            return
        command = parts[0].split("@")[0].lower()
        args = parts[1:]
        from_id = str(sender.get("from_id") or "")
        chat_id = str(sender.get("chat_id") or "")
        is_admin = is_admin_sender(sender, config)

        if command == "/start":
            if not from_id:
                return
            if is_admin:
                reply(
                    "🔒 Admin access.\n\n"
                    "Commands:\n"
                    "/licenses — toggle monitoring per device\n"
                    "/users — list people who asked for access\n"
                    "/approve <id> [name] — let a friend follow devices\n"
                    "/revoke <id> — remove a friend",
                    sender,
                )
                return
            with state_lock:
                registry = users_holder.setdefault("users", {})
                entry = registry.get(from_id)
                known = isinstance(entry, dict)
                if not known:
                    entry = {
                        "user_id": from_id,
                        "chat_id": chat_id or from_id,
                        "label": from_id,
                        "approved": False,
                        "subscriptions": [],
                    }
                    registry[from_id] = entry
                elif chat_id:
                    entry["chat_id"] = chat_id
                entry["seen_at"] = datetime.now(timezone.utc).isoformat()
                save_users(users_holder)
            if not known:
                logger.info("New Telegram user %s asked for access.", from_id)
            if entry.get("approved"):
                reply(
                    f"✅ Welcome back, {entry.get('label') or from_id}.\n\n"
                    f"Your ID: {from_id}\n"
                    "Send /addDevices to choose which devices you follow.",
                    sender,
                )
            else:
                reply(
                    f"👋 Welcome!\n\nYour ID: {from_id}\n\n"
                    f"Ask the admin to run:\n/approve {from_id}",
                    sender,
                )
            return

        if command == "/licenses":
            if not is_admin:
                logger.warning("Ignoring /licenses from non-admin chat %s.", chat_id or "?")
                return
            with state_lock:
                list_text, keyboard = build_licenses_list(holder)
                save_state(holder)
            if notifier:
                notifier.notify(list_text, buttons=keyboard)
            logger.info("/licenses list sent (%d license(s)).", len(holder.get("licenses", {})))
            return

        if command == "/users":
            if not is_admin:
                reply("Admin only.", sender)
                return
            with state_lock:
                entries = sorted(
                    (users_holder.get("users") or {}).values(),
                    key=lambda item: str((item or {}).get("user_id") or ""),
                )
            if not entries:
                reply(
                    "👥 No one yet.\n\nFriends should send /start to this bot first.",
                    sender,
                )
                return
            lines = ["👥 Users", ""]
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                mark = "🟢" if entry.get("approved") else "🟡"
                label = str(entry.get("label") or entry.get("user_id") or "?")[:24]
                count = len(entry.get("subscriptions") or [])
                state = "approved" if entry.get("approved") else "pending"
                lines.append(
                    f"{mark} {label} ({entry.get('user_id')}) — {count} device(s) — {state}"
                )
            reply("\n".join(lines), sender)
            return

        if command == "/approve":
            if not is_admin:
                reply("Admin only.", sender)
                return
            if not args or not args[0].lstrip("-").isdigit():
                reply("Usage: /approve <user_id> [name]", sender)
                return
            user_id = args[0]
            if user_id == admin_chat_id(config):
                reply("That is your own admin chat — you already see every device.", sender)
                return
            with state_lock:
                registry = users_holder.setdefault("users", {})
                entry = registry.get(user_id)
                if not isinstance(entry, dict):
                    entry = {"user_id": user_id}
                    registry[user_id] = entry
                entry["user_id"] = user_id
                entry["approved"] = True
                entry.setdefault("chat_id", user_id)
                entry.setdefault("subscriptions", [])
                label = " ".join(args[1:]).strip()
                if label:
                    entry["label"] = label
                else:
                    entry.setdefault("label", user_id)
                entry["approved_at"] = datetime.now(timezone.utc).isoformat()
                save_users(users_holder)
            reply(
                f"✅ Approved {entry.get('label')} ({user_id}).\n\n"
                "They can now send /addDevices to this bot.",
                sender,
            )
            logger.info("Telegram user %s approved.", user_id)
            return

        if command == "/revoke":
            if not is_admin:
                reply("Admin only.", sender)
                return
            if not args:
                reply("Usage: /revoke <user_id>", sender)
                return
            user_id = args[0]
            with state_lock:
                removed = (users_holder.get("users") or {}).pop(user_id, None)
                for info in holder.get("licenses", {}).values():
                    if isinstance(info, dict) and isinstance(info.get("users"), dict):
                        info["users"].pop(user_id, None)
                save_users(users_holder)
                save_state(holder)
            if not isinstance(removed, dict):
                reply(f"Unknown user {user_id}.", sender)
                return
            reply(
                f"🔴 Revoked {removed.get('label') or user_id} ({user_id}).",
                sender,
            )
            logger.info("Telegram user %s revoked.", user_id)
            return

        if command in ("/adddevices", "/mydevices", "/devices"):
            if is_admin:
                reply("You already receive updates for every device.", sender)
                return
            entry = resolve_user(users_holder, sender)
            if entry is None:
                ident = from_id or chat_id or "?"
                reply(
                    f"You are not approved yet.\n\nYour ID: {ident}\n\n"
                    f"Ask the admin to run:\n/approve {ident}",
                    sender,
                )
                return
            if chat_id and entry.get("chat_id") != chat_id:
                with state_lock:
                    entry["chat_id"] = chat_id
                    save_users(users_holder)
            page = int(args[0]) if args and args[0].isdigit() else 1
            user_id = str(entry.get("user_id") or from_id)
            with state_lock:
                list_text, keyboard, _, _ = build_subscribe_list(
                    holder, users_holder, user_id, page
                )
                save_state(holder)
            if notifier:
                notifier.notify(list_text, buttons=keyboard, chat_id=chat_id or None)
            logger.info(
                "/addDevices list sent to user %s (%d license(s)).",
                user_id,
                len(holder.get("licenses", {})),
            )
            return

        logger.debug("Ignoring unsupported command %r.", command)

    return handle_command


def run_loop(client, config, once=False, notifier=None):
    interval = max(int(config.get("poll_interval_seconds", 60)), 1)
    reminder_seconds = int(config.get("reminder_interval_minutes", 5)) * 60
    state_lock = threading.Lock()
    holder = load_state()
    users_holder = load_users()

    if notifier:
        notifier.start_callback_poller(
            make_action_handler(holder, users_holder, state_lock, notifier, config),
            make_command_handler(holder, users_holder, state_lock, notifier, config),
        )

    while True:
        started = time.monotonic()
        try:
            with state_lock:
                # Snapshot before the network fetch so button presses landing
                # mid-fetch cannot mutate what this cycle compares against.
                frozen_previous = {}
                for key, value in holder["licenses"].items():
                    clone = dict(value)
                    if isinstance(value.get("users"), dict):
                        clone["users"] = {
                            user_key: dict(user_record)
                            for user_key, user_record in value["users"].items()
                        }
                    frozen_previous[key] = clone

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
                    if "monitoring_enabled" in prev:
                        info["monitoring_enabled"] = prev["monitoring_enabled"]
                    if isinstance(prev.get("users"), dict):
                        info["users"] = prev["users"]
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
                            admin_alert = False
                        # Skip notification if monitoring is disabled for this license
                        elif not info.get("monitoring_enabled", True):
                            logger.info(
                                "License %s (%s) went offline but monitoring is disabled; no notification.",
                                short_id(license_id),
                                info["device_name"],
                            )
                            admin_alert = False
                        else:
                            admin_alert = True

                        user_targets = (
                            subscribed_users(
                                users_holder, info, want=lambda state: state != "ignored"
                            )
                            if notifier
                            else []
                        )
                        if not admin_alert and not user_targets:
                            continue

                        if admin_alert:
                            info["notification_state"] = "active"
                        info["offline_since"] = now_dt.isoformat()
                        info["last_reminder_sent"] = info["offline_since"]
                        if notifier:
                            offline_text = build_message(
                                "🔴 Device Offline",
                                info,
                                f"License:\n{short_id(license_id)}",
                            )
                            offline_buttons = ignore_keyboard(
                                get_or_create_action_id(holder, license_id)
                            )
                            if admin_alert:
                                outgoing.append({
                                    "kind": "send",
                                    "license_id": license_id,
                                    "text": offline_text,
                                    "buttons": offline_buttons,
                                    "track": True,
                                    "targets": [(None, None)],
                                })
                            for target_chat, target_user in user_targets:
                                outgoing.append({
                                    "kind": "send",
                                    "license_id": license_id,
                                    "text": offline_text,
                                    "buttons": offline_buttons,
                                    "track": True,
                                    "targets": [(target_chat, target_user)],
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
                        if notifier and info.get("monitoring_enabled", True):
                            online_text = build_message("🟢 Device Online", info, footer)
                            closed_text = RECOVERY_CLOSED_TEXT.format(
                                device_name=stale_device_name or info["device_name"],
                                license_short=license_short,
                            )
                            outgoing.append({
                                "kind": "send",
                                "license_id": license_id,
                                "text": online_text,
                                "buttons": None,
                                "track": False,
                                "targets": [(None, None)],
                            })
                            if stale_message_id:
                                outgoing.append({
                                    "kind": "edit",
                                    "message_id": stale_message_id,
                                    "text": closed_text,
                                })
                            # One copy per subscriber, plus in-place recovery edit.
                            for target_chat, target_user in subscribed_users(users_holder, info):
                                outgoing.append({
                                    "kind": "send",
                                    "license_id": license_id,
                                    "text": online_text,
                                    "buttons": None,
                                    "track": False,
                                    "targets": [(target_chat, target_user)],
                                })
                                user_record = (info.get("users") or {}).get(target_user)
                                user_message_id = (
                                    user_record.get("message_id")
                                    if isinstance(user_record, dict)
                                    else None
                                )
                                if user_message_id:
                                    outgoing.append({
                                        "kind": "edit",
                                        "message_id": user_message_id,
                                        "text": RECOVERY_CLOSED_TEXT.format(
                                            device_name=info["device_name"],
                                            license_short=license_short,
                                        ),
                                        "chat_id": target_chat,
                                    })
                        info["notification_state"] = "recovered"
                        for _, target_user in subscribed_users(users_holder, info):
                            set_user_alert_state(info, target_user, "recovered")
                        if minutes is not None:
                            logger.info(
                                "License %s back online after %d minute(s) offline.",
                                short_id(license_id),
                                minutes,
                            )

                for license_id, info in current.items():
                    if (
                        info["status"] != "offline"
                        or not info.get("monitoring_enabled", True)
                    ):
                        continue
                    admin_due = info.get("notification_state") == "active"
                    user_targets = (
                        subscribed_users(
                            users_holder, info, want=lambda state: state == "active"
                        )
                        if notifier
                        else []
                    )
                    if not admin_due and not user_targets:
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
                            license_short = short_id(license_id)
                            footer = f"License:\n{license_short}\n\nOffline for:\n{minutes} minutes"
                            reminder_text = build_message(
                                "⚠️ Device Offline Reminder", info, footer
                            )
                            reminder_buttons = ignore_keyboard(
                                get_or_create_action_id(holder, license_id)
                            )
                            if admin_due:
                                outgoing.append({
                                    "kind": "send",
                                    "license_id": license_id,
                                    "text": reminder_text,
                                    "buttons": reminder_buttons,
                                    "track": True,
                                    "targets": [(None, None)],
                                })
                            for target_chat, target_user in user_targets:
                                outgoing.append({
                                    "kind": "send",
                                    "license_id": license_id,
                                    "text": reminder_text,
                                    "buttons": reminder_buttons,
                                    "track": True,
                                    "targets": [(target_chat, target_user)],
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
                        notifier.edit_message(
                            item["text"], item["message_id"], chat_id=item.get("chat_id")
                        )
                        continue
                    for target_chat, target_user in item.get("targets") or [(None, None)]:
                        message_id = notifier.notify(
                            item["text"], buttons=item["buttons"], chat_id=target_chat
                        )
                        if not (item["track"] and message_id):
                            continue
                        with state_lock:
                            entry = holder["licenses"].get(item["license_id"])
                            if entry is None:
                                continue
                            if target_user is None:
                                entry["telegram_message_id"] = message_id
                            else:
                                records = entry.get("users")
                                if not isinstance(records, dict):
                                    records = {}
                                    entry["users"] = records
                                record = records.get(target_user)
                                if not isinstance(record, dict):
                                    record = {"alert_state": "active"}
                                    records[target_user] = record
                                record["message_id"] = message_id
                            save_state(holder)
        except AuthenticationError as exc:
            logger.error("Authentication requires manual attention: %s", exc)
            if notifier and not holder.get("auth_failure_notified", False):
                try:
                    auth_msg = (
                        "🔴 U Network Authentication Failed\n\n"
                        "The refresh token has expired or was rejected.\n\n"
                        "The monitor cannot refresh its access token and requires manual attention.\n\n"
                        "Please manually update/fix the refresh token and restart the monitor."
                    )
                    notifier.notify(auth_msg)
                    holder["auth_failure_notified"] = True
                    save_state(holder)
                    logger.info("Authentication failure notification sent via Telegram.")
                except Exception:
                    logger.exception("Failed to send authentication failure notification.")
            return 1
        except (ApiError, ConnectionError, requests.RequestException) as exc:
            logger.error("Poll cycle failed (will retry next cycle): %s", exc)
        except Exception:
            logger.exception("Unexpected error during poll cycle (will retry).")
        else:
            # Reset auth failure notification flag on successful poll cycle
            if holder.get("auth_failure_notified", False):
                holder["auth_failure_notified"] = False
                save_state(holder)

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

    # Reset auth failure notification flag on successful authentication
    state = load_state()
    if state.get("auth_failure_notified", False):
        state["auth_failure_notified"] = False
        save_state(state)

    try:
        return run_loop(client, config, once=args.once, notifier=TelegramNotifier(config))
    except (KeyboardInterrupt, StopRun):
        logger.info("Shutdown requested; exiting.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
