"""U Network device monitoring service (Phase 1).

Polls licenses_get_licenses, tracks per-device online state in state.json and
prints alerts when devices switch between online and offline.
"""

import argparse
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from auth import AuthManager, AuthenticationError
from unetwork import ApiError, UNetworkClient

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("UNETWORK_CONFIG_FILE", BASE_DIR / "config.json"))
STATE_PATH = Path(os.environ.get("UNETWORK_STATE_FILE", BASE_DIR / "state.json"))

logger = logging.getLogger("monitor")


class StopRun(Exception):
    pass


def _raise_stop(signum, frame):
    raise StopRun


def setup_logging():
    level = getattr(logging, os.environ.get("UNETWORK_LOG_LEVEL", "INFO").upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


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
        return {"devices": {}}
    try:
        with STATE_PATH.open(encoding="utf-8") as handle:
            state = json.load(handle)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not read %s (%s); starting with empty state.", STATE_PATH, exc)
        return {"devices": {}}
    if not isinstance(state, dict) or not isinstance(state.get("devices"), dict):
        return {"devices": {}}
    return state


def save_state(devices):
    payload = {
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "devices": devices,
    }
    tmp_path = STATE_PATH.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp_path, STATE_PATH)


def short_id(device_id):
    """Render an identifier as <first 4>...<last 4> (e.g. 0x35e...5678)."""
    text = str(device_id)
    if len(text) <= 12:
        return text
    return f"{text[:4]}...{text[-4:]}"


def format_label(info):
    """'0x35e...5678 (Name)' or just the short id when no name exists."""
    short = short_id(info["id"])
    name = info.get("name") or ""
    return f"{short} ({name})" if name else short


def snapshot_devices(licenses):
    """Extract {license id: {id, name, status}} from API license items."""
    devices = {}
    for item in licenses:
        device_id = item.get("id")
        if device_id is None:
            logger.warning("Skipping license entry without 'id': %s", str(item)[:120])
            continue
        devices[str(device_id)] = {
            "id": str(device_id),
            "name": item.get("deviceName") or "",
            "status": "online" if item.get("isOnline") else "offline",
        }
    return devices


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
            label = format_label(info) if isinstance(info, dict) and "id" in info else short_id(device_id)
            logger.warning(
                "Device %s no longer returned by the API; removing from tracked state.",
                label,
            )
            continue
        if was_online and entry["status"] == "offline":
            events.append((format_label(entry), "went offline"))
        elif not was_online and entry["status"] == "online":
            events.append((format_label(entry), "came online"))
        else:
            logger.debug("Device %s unchanged (%s).", format_label(entry), entry["status"])
    return events


def run_cycle(client, previous):
    licenses = list(client.iter_all_licenses())
    current = snapshot_devices(licenses)
    online_count = sum(1 for info in current.values() if info["status"] == "online")
    logger.info("Fetched %d device(s); %d online.", len(current), online_count)

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

    save_state(current)
    return current, events


def run_loop(client, config, once=False):
    interval = max(int(config.get("poll_interval_seconds", 60)), 1)
    previous = load_state()["devices"]

    while True:
        started = time.monotonic()
        try:
            current, events = run_cycle(client, previous)
            previous = current
            for label, event in events:
                print(f"Device {label} {event}", flush=True)
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
        return run_loop(client, config, once=args.once)
    except (KeyboardInterrupt, StopRun):
        logger.info("Shutdown requested; exiting.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
