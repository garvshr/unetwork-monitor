"""End-to-end routing tests that drive the real run_loop.

A fake API client and a fake Telegram notifier are used so no network calls are
made and no real messages are sent. Temp state/users files keep the developer's
own state.json untouched.
"""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import main as monitor

ADMIN_CHAT = "999000111"
USER_ID = "111"
LICENSE_ID = "lic-001"

CONFIG = {
    "telegram_chat_id": ADMIN_CHAT,
    "poll_interval_seconds": 60,
    "reminder_interval_minutes": 5,
}


class FakeClient:
    def __init__(self, items):
        self._items = items

    def iter_all_licenses(self):
        return iter(list(self._items))


class FakeNotifier:
    """Records everything instead of calling the Telegram API."""

    def __init__(self):
        self.sent = []
        self.edited = []
        self._next = 100
        self.action_handler = None
        self.command_handler = None

    @property
    def active(self):
        return True

    def start_callback_poller(self, handler, command_handler=None):
        self.action_handler = handler
        self.command_handler = command_handler

    def stop(self):
        pass

    def notify(self, text, buttons=None, chat_id=None):
        self._next += 1
        self.sent.append({"chat_id": chat_id, "text": text, "buttons": buttons})
        return self._next

    def edit_message(self, text, message_id, buttons=None, chat_id=None):
        self.edited.append({"chat_id": chat_id, "message_id": message_id, "text": text})
        return True


def license_item(online, name="Pixel 8"):
    return {"id": LICENSE_ID, "deviceName": name, "isOnline": online, "totalCount": 1}


def make_user(user_id, approved=True, subscriptions=None):
    return {
        "user_id": str(user_id),
        "chat_id": str(user_id),
        "label": str(user_id),
        "approved": approved,
        "subscriptions": list(subscriptions if subscriptions is not None else [LICENSE_ID]),
    }


class RunLoopTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._orig_state = monitor.STATE_PATH
        self._orig_users = monitor.USERS_PATH
        monitor.STATE_PATH = self.root / "state.json"
        monitor.USERS_PATH = self.root / "users.json"

    def tearDown(self):
        monitor.STATE_PATH = self._orig_state
        monitor.USERS_PATH = self._orig_users
        self._tmp.cleanup()

    # -- helpers ---------------------------------------------------------

    def cycle(self, online, users=None):
        """Run exactly one poll cycle and return the fake notifier."""
        if users is not None:
            monitor.save_users({"users": users})
        notifier = FakeNotifier()
        result = monitor.run_loop(
            FakeClient([license_item(online)]), CONFIG, once=True, notifier=notifier
        )
        self.assertEqual(result, 0)
        return notifier

    def read_state(self):
        return json.loads(monitor.STATE_PATH.read_text(encoding="utf-8"))

    def read_users(self):
        if not monitor.USERS_PATH.is_file():
            return {"users": {}}
        return json.loads(monitor.USERS_PATH.read_text(encoding="utf-8"))

    def license_state(self):
        return self.read_state()["licenses"][LICENSE_ID]

    def rewrite_license_state(self, **updates):
        state = self.read_state()
        state["licenses"][LICENSE_ID].update(updates)
        monitor.STATE_PATH.write_text(json.dumps(state), encoding="utf-8")

    @staticmethod
    def sent_to(notifier, chat_id):
        return [msg for msg in notifier.sent if msg["chat_id"] == chat_id]

    @staticmethod
    def containing(notifier, needle):
        return [msg for msg in notifier.sent if needle in msg["text"]]


class AdminOnlyRegressionTests(RunLoopTestCase):
    """The original single-user behaviour must be byte-for-byte intact."""

    def test_first_run_is_a_silent_baseline(self):
        notifier = self.cycle(True)
        self.assertEqual(notifier.sent, [])
        self.assertIn(LICENSE_ID, self.read_state()["licenses"])

    def test_offline_alert_goes_to_admin_only(self):
        self.cycle(True)
        notifier = self.cycle(False)

        self.assertEqual(len(notifier.sent), 1)
        message = notifier.sent[0]
        self.assertIsNone(message["chat_id"])
        self.assertIn("Device Offline", message["text"])
        self.assertIn(LICENSE_ID, self.license_state()["license_id"])
        self.assertTrue(
            message["buttons"]["inline_keyboard"][0][0]["callback_data"].startswith(
                "ignore:"
            )
        )
        self.assertTrue(self.license_state()["telegram_message_id"])
        self.assertEqual(self.license_state()["notification_state"], "active")

    def test_recovery_edits_the_original_offline_message(self):
        self.cycle(True)
        self.cycle(False)
        notifier = self.cycle(True)

        online = self.containing(notifier, "Device Online")
        self.assertEqual(len(online), 1)
        self.assertIsNone(online[0]["chat_id"])
        self.assertIn("Offline duration", online[0]["text"])

        self.assertEqual(len(notifier.edited), 1)
        self.assertIsNone(notifier.edited[0]["chat_id"])
        self.assertIn("Device Back Online", notifier.edited[0]["text"])
        self.assertEqual(self.license_state()["notification_state"], "recovered")

    def test_reminder_goes_to_admin_only(self):
        self.cycle(True)
        self.cycle(False)  # starts the offline clock
        self.rewrite_license_state(
            offline_since=(datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(),
            last_reminder_sent=(datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(),
        )
        notifier = self.cycle(False)

        reminders = self.containing(notifier, "Offline Reminder")
        self.assertEqual(len(reminders), 1)
        self.assertIsNone(reminders[0]["chat_id"])
        self.assertIn("10 minutes", reminders[0]["text"])

    def test_disabled_license_notifies_nobody(self):
        self.cycle(True)
        self.rewrite_license_state(monitoring_enabled=False)
        notifier = self.cycle(False)
        self.assertEqual(notifier.sent, [])

    def test_admin_licenses_command_still_posts_the_global_list(self):
        notifier = self.cycle(True)
        notifier.command_handler(
            "/licenses", {"from_id": ADMIN_CHAT, "chat_id": ADMIN_CHAT}
        )
        lists = self.containing(notifier, "Monitored Licenses")
        self.assertEqual(len(lists), 1)
        self.assertIsNone(lists[0]["chat_id"])

    def test_admin_licenses_command_is_rejected_for_other_senders(self):
        notifier = self.cycle(True)
        notifier.command_handler("/licenses", {"from_id": USER_ID, "chat_id": USER_ID})
        self.assertEqual(self.containing(notifier, "Monitored Licenses"), [])


class SubscriberRoutingTests(RunLoopTestCase):
    def test_subscribed_user_gets_a_copy_of_the_offline_alert(self):
        users = {USER_ID: make_user(USER_ID)}
        self.cycle(True, users=users)
        notifier = self.cycle(False, users=users)

        self.assertEqual(len(self.sent_to(notifier, None)), 1)
        user_messages = self.sent_to(notifier, USER_ID)
        self.assertEqual(len(user_messages), 1)
        self.assertIn("Device Offline", user_messages[0]["text"])

        state = self.license_state()
        self.assertTrue(state["telegram_message_id"])
        self.assertTrue(state["users"][USER_ID]["message_id"])

    def test_subscribed_user_gets_recovery_notice_and_message_edit(self):
        users = {USER_ID: make_user(USER_ID)}
        self.cycle(True, users=users)
        self.cycle(False, users=users)
        notifier = self.cycle(True, users=users)

        user_online = [
            msg for msg in self.sent_to(notifier, USER_ID) if "Device Online" in msg["text"]
        ]
        self.assertEqual(len(user_online), 1)

        user_edits = [edit for edit in notifier.edited if edit["chat_id"] == USER_ID]
        self.assertEqual(len(user_edits), 1)
        self.assertIn("Device Back Online", user_edits[0]["text"])

    def test_subscribed_user_gets_reminders(self):
        users = {USER_ID: make_user(USER_ID)}
        self.cycle(True, users=users)
        self.cycle(False, users=users)
        self.rewrite_license_state(
            offline_since=(datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(),
            last_reminder_sent=(datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(),
        )
        notifier = self.cycle(False, users=users)

        self.assertEqual(len(self.containing(notifier, "Offline Reminder")), 2)
        self.assertEqual(len(self.sent_to(notifier, USER_ID)), 1)

    def test_ignored_user_is_muted_but_admin_still_gets_the_alert(self):
        users = {USER_ID: make_user(USER_ID)}
        self.cycle(True, users=users)
        self.cycle(False, users=users)
        self.rewrite_license_state(
            users={USER_ID: {"alert_state": "ignored"}},
            notification_state="active",
            offline_since=(datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(),
            last_reminder_sent=(datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(),
        )
        notifier = self.cycle(False, users=users)

        self.assertEqual(self.sent_to(notifier, USER_ID), [])
        reminders = self.containing(notifier, "Offline Reminder")
        self.assertEqual(len(reminders), 1)
        self.assertIsNone(reminders[0]["chat_id"])

    def test_unapproved_user_never_receives_anything(self):
        users = {USER_ID: make_user(USER_ID, approved=False)}
        self.cycle(True, users=users)
        notifier = self.cycle(False, users=users)
        self.assertEqual(self.sent_to(notifier, USER_ID), [])
        self.assertEqual(len(self.sent_to(notifier, None)), 1)

    def test_user_not_following_the_license_receives_nothing(self):
        users = {USER_ID: make_user(USER_ID, subscriptions=[])}
        self.cycle(True, users=users)
        notifier = self.cycle(False, users=users)
        self.assertEqual(self.sent_to(notifier, USER_ID), [])
        self.assertEqual(len(self.sent_to(notifier, None)), 1)

    def test_following_a_different_license_receives_nothing(self):
        users = {USER_ID: make_user(USER_ID, subscriptions=["other-license"])}
        self.cycle(True, users=users)
        notifier = self.cycle(False, users=users)
        self.assertEqual(self.sent_to(notifier, USER_ID), [])


class ButtonAuthorizationTests(RunLoopTestCase):
    def _offline_notifier(self, users):
        self.cycle(True, users=users)
        notifier = self.cycle(False, users=users)
        action_id = next(iter(self.read_state()["telegram_actions"]))
        return notifier, action_id

    def test_user_ignore_mutes_only_that_user(self):
        notifier, action_id = self._offline_notifier({USER_ID: make_user(USER_ID)})
        toast, jobs = notifier.action_handler(
            "ignore", action_id, 500, {"from_id": USER_ID, "chat_id": USER_ID}
        )

        self.assertIn("Ignored", toast)
        state = self.license_state()
        self.assertEqual(state["users"][USER_ID]["alert_state"], "ignored")
        self.assertEqual(state["notification_state"], "active")

        for job in jobs:
            job()
        self.assertEqual(len(notifier.edited), 1)
        self.assertEqual(notifier.edited[0]["chat_id"], USER_ID)
        self.assertIn("Ignored", notifier.edited[0]["text"])

    def test_admin_ignore_does_not_touch_the_users_state(self):
        notifier, action_id = self._offline_notifier({USER_ID: make_user(USER_ID)})
        toast, _ = notifier.action_handler(
            "ignore", action_id, 400, {"from_id": ADMIN_CHAT, "chat_id": ADMIN_CHAT}
        )

        self.assertIn("Ignored", toast)
        state = self.license_state()
        self.assertEqual(state["notification_state"], "ignored")
        # The subscriber's own alert state must be left alone.
        self.assertEqual(state["users"][USER_ID]["alert_state"], "active")
        self.assertTrue(state["users"][USER_ID]["message_id"])

    def test_unapproved_sender_cannot_press_buttons(self):
        notifier, action_id = self._offline_notifier({USER_ID: make_user(USER_ID)})
        before = self.read_state()
        toast, jobs = notifier.action_handler(
            "ignore", action_id, 300, {"from_id": "666", "chat_id": "666"}
        )

        self.assertEqual(toast, "You are not approved.")
        self.assertEqual(jobs, [])
        self.assertEqual(self.read_state(), before)

    def test_admin_pressing_a_subscriber_button_is_refused(self):
        notifier, action_id = self._offline_notifier({USER_ID: make_user(USER_ID)})
        before = self.read_state()
        toast, _ = notifier.action_handler(
            "sub", f"{action_id}:1", 200, {"from_id": ADMIN_CHAT, "chat_id": ADMIN_CHAT}
        )
        self.assertEqual(self.read_state(), before)
        self.assertIn("already see every device", toast)

    def test_sub_button_toggles_a_subscription(self):
        users = {USER_ID: make_user(USER_ID, subscriptions=[])}
        notifier, action_id = self._offline_notifier(users)

        toast, jobs = notifier.action_handler(
            "sub", f"{action_id}:1", 200, {"from_id": USER_ID, "chat_id": USER_ID}
        )
        self.assertIn("now receive", toast)
        self.assertEqual(self.read_users()["users"][USER_ID]["subscriptions"], [LICENSE_ID])
        for job in jobs:
            job()
        self.assertEqual(len(notifier.edited), 1)
        self.assertEqual(notifier.edited[0]["chat_id"], USER_ID)

        toast, _ = notifier.action_handler(
            "sub", f"{action_id}:1", 200, {"from_id": USER_ID, "chat_id": USER_ID}
        )
        self.assertIn("stop receiving", toast)
        self.assertEqual(self.read_users()["users"][USER_ID]["subscriptions"], [])

    def test_user_monitor_button_restores_their_alerts(self):
        notifier, action_id = self._offline_notifier({USER_ID: make_user(USER_ID)})
        notifier.action_handler(
            "ignore", action_id, 500, {"from_id": USER_ID, "chat_id": USER_ID}
        )
        self.assertEqual(
            self.license_state()["users"][USER_ID]["alert_state"], "ignored"
        )

        notifier.sent.clear()
        notifier.edited.clear()
        toast, jobs = notifier.action_handler(
            "monitor", action_id, 500, {"from_id": USER_ID, "chat_id": USER_ID}
        )
        self.assertIn("Monitoring started", toast)
        state = self.license_state()
        self.assertEqual(state["users"][USER_ID]["alert_state"], "active")
        # The admin's own state is untouched.
        self.assertEqual(state["notification_state"], "active")

        for job in jobs:
            job()
        self.assertEqual(len(notifier.edited), 1)
        self.assertEqual(notifier.edited[0]["chat_id"], USER_ID)
        self.assertIn("Monitoring Started", notifier.edited[0]["text"])
        fresh_alerts = [
            msg
            for msg in notifier.sent
            if msg["chat_id"] == USER_ID and "Device Offline" in msg["text"]
        ]
        self.assertEqual(len(fresh_alerts), 1)
        self.assertTrue(self.license_state()["users"][USER_ID]["message_id"])

    def test_user_monitor_before_ignore_is_refused(self):
        notifier, action_id = self._offline_notifier({USER_ID: make_user(USER_ID)})
        toast, jobs = notifier.action_handler(
            "monitor", action_id, 500, {"from_id": USER_ID, "chat_id": USER_ID}
        )
        self.assertIn("ignore the alert first", toast)
        self.assertEqual(jobs, [])
        self.assertEqual(
            self.license_state()["users"][USER_ID]["alert_state"], "active"
        )

    def test_page_button_rerenders_the_device_list_for_the_user(self):
        notifier, action_id = self._offline_notifier(
            {USER_ID: make_user(USER_ID, subscriptions=[])}
        )
        notifier.edited.clear()
        toast, jobs = notifier.action_handler(
            "page", "2", 700, {"from_id": USER_ID, "chat_id": USER_ID}
        )
        self.assertIsNone(toast)
        for job in jobs:
            job()
        self.assertEqual(len(notifier.edited), 1)
        self.assertEqual(notifier.edited[0]["chat_id"], USER_ID)
        self.assertEqual(notifier.edited[0]["message_id"], 700)
        self.assertIn("My Devices", notifier.edited[0]["text"])


class CommandFlowTests(RunLoopTestCase):
    def test_admin_start_lists_the_admin_commands(self):
        notifier = self.cycle(True)
        notifier.command_handler(
            "/start", {"from_id": ADMIN_CHAT, "chat_id": ADMIN_CHAT}
        )
        replies = self.sent_to(notifier, ADMIN_CHAT)
        self.assertEqual(len(replies), 1)
        self.assertIn("Admin access", replies[0]["text"])
        self.assertIn("/approve", replies[0]["text"])
        # An admin /start must not register them as a subscriber.
        self.assertNotIn(ADMIN_CHAT, self.read_users()["users"])

    def test_full_onboarding_flow(self):
        notifier = self.cycle(True)
        command = notifier.command_handler

        # 1. A stranger says /start -> their id is captured and echoed back.
        command("/start", {"from_id": "555", "chat_id": "555"})
        self.assertEqual(self.read_users()["users"]["555"]["approved"], False)
        start_reply = self.sent_to(notifier, "555")
        self.assertEqual(len(start_reply), 1)
        self.assertIn("555", start_reply[0]["text"])

        # 2. A non-admin cannot approve.
        command("/approve 555 Bob", {"from_id": USER_ID, "chat_id": USER_ID})
        self.assertFalse(self.read_users()["users"]["555"]["approved"])

        # 3. The admin approves.
        command(
            "/approve 555 Bob", {"from_id": ADMIN_CHAT, "chat_id": ADMIN_CHAT}
        )
        entry = self.read_users()["users"]["555"]
        self.assertTrue(entry["approved"])
        self.assertEqual(entry["label"], "Bob")

        # 4. They cannot /addDevices until approved... (already approved here)
        command("/addDevices", {"from_id": "555", "chat_id": "555"})
        lists = [
            msg
            for msg in notifier.sent
            if msg["chat_id"] == "555" and "My Devices" in msg["text"]
        ]
        self.assertEqual(len(lists), 1)
        self.assertEqual(
            lists[0]["buttons"]["inline_keyboard"][0][0]["callback_data"],
            f"sub:1:1",
        )

        # 5. /users shows them as approved.
        command("/users", {"from_id": ADMIN_CHAT, "chat_id": ADMIN_CHAT})
        listing = self.containing(notifier, "👥 Users")
        self.assertTrue(any("Bob" in msg["text"] for msg in listing))

        # 6. /revoke removes them and their device picks.
        command("/revoke 555", {"from_id": ADMIN_CHAT, "chat_id": ADMIN_CHAT})
        self.assertNotIn("555", self.read_users()["users"])

    def test_unapproved_user_gets_a_clear_rejection(self):
        notifier = self.cycle(True)
        notifier.command_handler("/addDevices", {"from_id": "777", "chat_id": "777"})
        replies = self.sent_to(notifier, "777")
        self.assertEqual(len(replies), 1)
        self.assertIn("not approved", replies[0]["text"])
        self.assertIn("/approve 777", replies[0]["text"])

    def test_admin_is_redirected_away_from_adddevices(self):
        notifier = self.cycle(True)
        notifier.command_handler(
            "/addDevices", {"from_id": ADMIN_CHAT, "chat_id": ADMIN_CHAT}
        )
        replies = self.sent_to(notifier, ADMIN_CHAT)
        self.assertEqual(len(replies), 1)
        self.assertIn("already receive updates for every device", replies[0]["text"])

    def test_approving_the_admin_chat_is_refused(self):
        notifier = self.cycle(True)
        notifier.command_handler(
            f"/approve {ADMIN_CHAT}", {"from_id": ADMIN_CHAT, "chat_id": ADMIN_CHAT}
        )
        self.assertNotIn(ADMIN_CHAT, self.read_users()["users"])
        replies = self.sent_to(notifier, ADMIN_CHAT)
        self.assertTrue(any("admin chat" in msg["text"] for msg in replies))

    def test_revoked_user_loses_their_device_state(self):
        users = {USER_ID: make_user(USER_ID)}
        self.cycle(True, users=users)
        self.cycle(False, users=users)
        self.assertIn(USER_ID, self.license_state()["users"])

        notifier = self.cycle(False, users=users)
        notifier.command_handler(
            f"/revoke {USER_ID}", {"from_id": ADMIN_CHAT, "chat_id": ADMIN_CHAT}
        )
        self.assertNotIn(USER_ID, self.license_state().get("users", {}))
        self.assertNotIn(USER_ID, self.read_users()["users"])


if __name__ == "__main__":
    unittest.main()
