"""Telegram Bot API notifications and button-callback handling."""

import json
import logging
import os
import sys
import threading
import time
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"


class TelegramNotifier:
    """Sends messages through the Telegram Bot API; failures never raise."""

    def __init__(self, config, session=None):
        self._token = (
            os.environ.get("UNETWORK_TELEGRAM_TOKEN")
            or config.get("telegram_bot_token")
            or ""
        )
        self._chat_id = (
            os.environ.get("UNETWORK_TELEGRAM_CHAT_ID")
            or config.get("telegram_chat_id")
            or ""
        )
        self._enabled = bool(config.get("telegram_enabled", True))
        self._timeout = float(config.get("request_timeout_seconds", 30))
        self._session = session or requests.Session()
        self._callback_thread = None
        self._stop_event = threading.Event()

        if self._enabled and self._token and self._chat_id:
            logger.info("Telegram notifications enabled.")
        elif self._enabled:
            logger.warning(
                "Telegram enabled but bot token/chat id not set; notifications will be skipped."
            )

    @property
    def active(self):
        return self._enabled and bool(self._token) and bool(self._chat_id)

    def notify(self, text, buttons=None):
        """Send text; returns the Telegram message_id on success, else None."""
        if not self.active:
            if self._enabled:
                logger.warning("Skipping Telegram notification: token/chat id missing.")
            return None
        return self._send(text, buttons)

    def _send(self, text, buttons=None):
        url = f"{API_BASE}/bot{self._token}/sendMessage"
        payload = {"chat_id": self._chat_id, "text": text}
        if buttons:
            payload["reply_markup"] = buttons
        try:
            response = self._session.post(url, json=payload, timeout=self._timeout)
        except requests.RequestException as exc:
            logger.error("Telegram notification failed: %s", exc)
            return None
        if response.status_code != 200:
            logger.error(
                "Telegram notification failed (HTTP %s): %s",
                response.status_code,
                response.text[:200],
            )
            return None
        try:
            message_id = response.json()["result"]["message_id"]
        except (ValueError, KeyError, TypeError):
            logger.warning("Telegram send succeeded but message_id missing from response.")
            return None
        logger.info("Telegram notification sent (message_id=%s).", message_id)
        return message_id

    def edit_message(self, text, message_id, buttons=None):
        """Edit an existing message; omitting buttons removes the keyboard."""
        payload = {
            "chat_id": self._chat_id,
            "message_id": message_id,
            "text": text,
        }
        if buttons:
            payload["reply_markup"] = buttons
        try:
            response = self._session.post(
                f"{API_BASE}/bot{self._token}/editMessageText", json=payload, timeout=self._timeout
            )
        except requests.RequestException as exc:
            logger.error("Telegram edit failed for message %s: %s", message_id, exc)
            return False
        if response.status_code == 200:
            logger.info("Telegram message %s edited.", message_id)
            return True
        logger.error(
            "Telegram edit failed for message %s (HTTP %s): %s",
            message_id,
            response.status_code,
            response.text[:150],
        )
        return False

    def start_callback_poller(self, handler, command_handler=None):
        """Begin a daemon thread that dispatches inline-button presses to handler.

        handler(action, license_ref, message_id) must return a tuple
        (toast_text, followup_jobs) or None. The callback query is answered
        with toast_text before any followup job (message edits/sends) runs.

        followup_jobs is a list of callables to execute after the callback is acknowledged.

        command_handler(text) optionally handles plain-text commands such as /licenses.
        """
        if self._callback_thread is not None:
            return
        if not self.active:
            logger.warning("Telegram callback listener not started: token/chat id missing.")
            return
        self._stop_event.clear()
        self._callback_thread = threading.Thread(
            target=self._poll_loop, args=(handler, command_handler),
            name="telegram-callbacks", daemon=True
        )
        self._callback_thread.start()
        logger.info("Telegram callback listener started.")

    def stop(self):
        self._stop_event.set()

    def _poll_loop(self, handler, command_handler=None):
        url = f"{API_BASE}/bot{self._token}/getUpdates"
        params = {"timeout": 25, "offset": 0}
        while not self._stop_event.is_set():
            try:
                response = self._session.get(url, params=params, timeout=self._timeout + 30)
            except requests.RequestException as exc:
                logger.error("Telegram getUpdates failed: %s", exc)
                time.sleep(10)
                continue
            if response.status_code != 200:
                logger.error(
                    "Telegram getUpdates failed (HTTP %s): %s",
                    response.status_code,
                    response.text[:150],
                )
                time.sleep(10)
                continue
            try:
                updates = response.json().get("result", [])
            except ValueError:
                logger.error("Telegram getUpdates returned malformed JSON.")
                continue
            for update in updates:
                params["offset"] = update["update_id"] + 1
                incoming = update.get("message") or {}
                incoming_text = (incoming.get("text") or "").strip()
                if incoming_text.startswith("/licenses"):
                    if command_handler:
                        try:
                            command_handler(incoming_text)
                        except Exception:
                            logger.exception("Command handler failed for /licenses")
                    continue
                query = update.get("callback_query")
                if not query:
                    continue
                action, _, license_ref = (query.get("data") or "").partition(":")
                if action not in ("ignore", "monitor", "start", "toggle") or not license_ref:
                    logger.warning("Ignoring unsupported callback data: %r", query.get("data"))
                    self._answer_callback(query.get("id"))
                    continue
                message_id = (query.get("message") or {}).get("message_id")
                logger.info(
                    "Telegram button pressed: %s for license %s", action, short_tag(license_ref)
                )
                try:
                    result = handler(action, license_ref, message_id)
                except Exception:
                    logger.exception("Callback handler failed for %r", query.get("data"))
                    self._answer_callback(
                        query.get("id"),
                        text="Action failed. Please try again.",
                        show_alert=True,
                    )
                    continue
                toast_text = None
                followup_jobs = []
                if isinstance(result, tuple):
                    toast_text, followup_jobs = result
                elif isinstance(result, str):
                    toast_text = result
                self._answer_callback(query.get("id"), text=toast_text)
                for job in followup_jobs or []:
                    try:
                        job()
                    except Exception:
                        logger.exception("Telegram followup job failed")

    def _answer_callback(self, callback_query_id, text=None, show_alert=False):
        """Acknowledge a callback. Telegram accepts only one answer per query;
        follow-up answers (e.g. error notices after the initial ack) are best-effort."""
        if not callback_query_id:
            return
        payload = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
            payload["show_alert"] = show_alert
        try:
            response = self._session.post(
                f"{API_BASE}/bot{self._token}/answerCallbackQuery",
                json=payload,
                timeout=self._timeout,
            )
            if response.status_code != 200:
                logger.debug(
                    "answerCallbackQuery not applied (HTTP %s): %s",
                    response.status_code,
                    response.text[:120],
                )
        except requests.RequestException as exc:
            logger.debug("answerCallbackQuery failed: %s", exc)


def short_tag(license_id):
    text = str(license_id)
    if len(text) <= 12:
        return text
    return f"{text[:4]}...{text[-4:]}"


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s | %(message)s")
    config_path = Path(os.environ.get("UNETWORK_CONFIG_FILE", Path(__file__).resolve().parent / "config.json"))
    with config_path.open(encoding="utf-8") as handle:
        cfg = json.load(handle)
    message = " ".join(sys.argv[1:]) or "U Network monitor: Telegram test message"
    sys.exit(0 if TelegramNotifier(cfg).notify(message) else 1)