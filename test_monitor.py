"""Unit tests for the routing and helper logic in main.py."""

import tempfile
import unittest
from pathlib import Path

import main as monitor

ADMIN = "999000111"


def admin_config():
    return {"telegram_chat_id": ADMIN}


def make_user(user_id, approved=True, subscriptions=None, chat_id=None, label=None):
    return {
        "user_id": str(user_id),
        "chat_id": str(chat_id if chat_id is not None else user_id),
        "label": label if label is not None else str(user_id),
        "approved": approved,
        "subscriptions": list(subscriptions or []),
    }


def make_license(license_id="0xabc123", name="Pixel 8", status="offline", **extra):
    record = {
        "license_id": license_id,
        "device_name": name,
        "status": status,
        "notification_state": "active",
        "monitoring_enabled": True,
    }
    record.update(extra)
    return record


def make_holder(records):
    return {"licenses": records, "telegram_actions": {}}


class IdentifierTests(unittest.TestCase):
    def test_short_id_leaves_short_values_alone(self):
        self.assertEqual(monitor.short_id("0x1234"), "0x1234")

    def test_short_id_keeps_first_four_and_last_four(self):
        self.assertEqual(monitor.short_id("0x35e0000005678"), "0x35...5678")

    def test_format_label_includes_device_name(self):
        info = {"license_id": "0x35e0000005678", "device_name": "Galaxy M21"}
        self.assertEqual(monitor.format_label(info), "0x35...5678 (Galaxy M21)")

    def test_format_label_without_device_name(self):
        info = {"license_id": "0x35e0000005678", "device_name": ""}
        self.assertEqual(monitor.format_label(info), "0x35...5678")

    def test_action_id_is_stable_and_increments(self):
        holder = {"licenses": {}, "telegram_actions": {}}
        first = monitor.get_or_create_action_id(holder, "lic-a")
        again = monitor.get_or_create_action_id(holder, "lic-a")
        second = monitor.get_or_create_action_id(holder, "lic-b")
        self.assertEqual(first, again)
        self.assertNotEqual(first, second)


class DetectChangesTests(unittest.TestCase):
    def test_online_to_offline_emits_went_offline(self):
        previous = {"lic": {"license_id": "lic", "device_name": "P", "status": "online"}}
        current = {"lic": {"license_id": "lic", "device_name": "P", "status": "offline"}}
        events = monitor.detect_changes(previous, current)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][1], "went offline")
        self.assertEqual(events[0][2], "lic")

    def test_offline_to_online_emits_came_online(self):
        previous = {"lic": {"license_id": "lic", "device_name": "P", "status": "offline"}}
        current = {"lic": {"license_id": "lic", "device_name": "P", "status": "online"}}
        events = monitor.detect_changes(previous, current)
        self.assertEqual([event for _, event, _ in events], ["came online"])

    def test_unchanged_status_emits_nothing(self):
        previous = {"lic": {"license_id": "lic", "device_name": "P", "status": "online"}}
        current = {"lic": {"license_id": "lic", "device_name": "P", "status": "online"}}
        self.assertEqual(monitor.detect_changes(previous, current), [])


class AdminGateTests(unittest.TestCase):
    def test_from_id_matching_admin_is_admin(self):
        sender = {"from_id": ADMIN, "chat_id": ADMIN}
        self.assertTrue(monitor.is_admin_sender(sender, admin_config()))

    def test_only_chat_id_matching_admin_is_admin(self):
        sender = {"from_id": "111", "chat_id": ADMIN}
        self.assertTrue(monitor.is_admin_sender(sender, admin_config()))

    def test_other_user_is_not_admin(self):
        sender = {"from_id": "111", "chat_id": "111"}
        self.assertFalse(monitor.is_admin_sender(sender, admin_config()))

    def test_no_configured_admin_id_means_nobody_is_admin(self):
        sender = {"from_id": ADMIN, "chat_id": ADMIN}
        self.assertFalse(monitor.is_admin_sender(sender, {"telegram_chat_id": ""}))


class UserRegistryTests(unittest.TestCase):
    def test_approved_user_requires_approved_flag(self):
        users = {
            "users": {
                "111": make_user("111", approved=False),
                "222": make_user("222", approved=True),
            }
        }
        self.assertIsNone(monitor.approved_user(users, "111"))
        self.assertIsNotNone(monitor.approved_user(users, "222"))

    def test_approved_user_returns_none_for_unknown_id(self):
        self.assertIsNone(monitor.approved_user({"users": {}}, "404"))

    def test_resolve_user_falls_back_to_chat_id(self):
        users = {"users": {"222": make_user("222", chat_id="222")}}
        sender = {"from_id": "", "chat_id": "222"}
        self.assertIsNotNone(monitor.resolve_user(users, sender))

    def test_resolve_user_rejects_unapproved_sender(self):
        users = {"users": {"111": make_user("111", approved=False)}}
        sender = {"from_id": "111", "chat_id": "111"}
        self.assertIsNone(monitor.resolve_user(users, sender))


class PerUserAlertStateTests(unittest.TestCase):
    def test_defaults_to_active_when_no_record(self):
        self.assertEqual(monitor.user_alert_state({}, "111"), "active")

    def test_defaults_to_active_when_record_has_no_state(self):
        info = {"users": {"111": {"message_id": 5}}}
        self.assertEqual(monitor.user_alert_state(info, "111"), "active")

    def test_reads_stored_state(self):
        info = {"users": {"111": {"alert_state": "ignored"}}}
        self.assertEqual(monitor.user_alert_state(info, "111"), "ignored")

    def test_set_creates_record_and_updates_state(self):
        info = {}
        monitor.set_user_alert_state(info, "111", "recovered")
        self.assertEqual(info["users"]["111"]["alert_state"], "recovered")

    def test_set_does_not_reset_other_record_fields(self):
        info = {"users": {"111": {"alert_state": "active", "message_id": 9}}}
        monitor.set_user_alert_state(info, "111", "recovered")
        self.assertEqual(info["users"]["111"]["message_id"], 9)


class SubscriptionTests(unittest.TestCase):
    def test_toggle_adds_then_removes(self):
        entry = make_user("111")
        self.assertTrue(monitor.toggle_subscription(entry, "lic-1"))
        self.assertEqual(entry["subscriptions"], ["lic-1"])
        self.assertFalse(monitor.toggle_subscription(entry, "lic-1"))
        self.assertEqual(entry["subscriptions"], [])

    def test_toggle_preserves_other_subscriptions(self):
        entry = make_user("111", subscriptions=["lic-1", "lic-2"])
        monitor.toggle_subscription(entry, "lic-3")
        self.assertEqual(entry["subscriptions"], ["lic-1", "lic-2", "lic-3"])

    def test_toggle_is_idempotent_on_string_vs_original_key(self):
        entry = make_user("111", subscriptions=["lic-1"])
        self.assertFalse(monitor.toggle_subscription(entry, "lic-1"))


class RoutingTests(unittest.TestCase):
    def _users(self, *entries):
        return {"users": {entry["user_id"]: entry for entry in entries}}

    def test_admin_alone_when_no_users_follow(self):
        info = make_license()
        users = self._users()
        self.assertEqual(
            monitor.subscribed_users(users, info),
            [],
        )

    def test_subscription_matching_is_by_license_only(self):
        info = make_license()
        users = self._users(make_user("111", subscriptions=["0xabc123"]))
        self.assertEqual(monitor.subscribed_users(users, info), [("111", "111")])

    def test_followers_are_matched_by_license_id(self):
        info = make_license(license_id="lic-1")
        users = self._users(
            make_user("111", subscriptions=["lic-1"]),
            make_user("222", subscriptions=["lic-2"]),
        )
        self.assertEqual(monitor.subscribed_users(users, info), [("111", "111")])

    def test_unapproved_users_never_receive(self):
        info = make_license(license_id="lic-1")
        users = self._users(make_user("111", approved=False, subscriptions=["lic-1"]))
        self.assertEqual(monitor.subscribed_users(users, info), [])

    def test_offline_filter_excludes_ignored_followers(self):
        info = make_license(license_id="lic-1", users={"111": {"alert_state": "ignored"}})
        users = self._users(make_user("111", subscriptions=["lic-1"]))
        self.assertEqual(
            monitor.subscribed_users(users, info, want=lambda state: state != "ignored"),
            [],
        )

    def test_reminder_filter_only_keeps_active_followers(self):
        info = make_license(
            license_id="lic-1",
            users={
                "111": {"alert_state": "active"},
                "222": {"alert_state": "ignored"},
                "333": {"alert_state": "recovered"},
            },
        )
        users = self._users(
            make_user("111", subscriptions=["lic-1"]),
            make_user("222", subscriptions=["lic-1"]),
            make_user("333", subscriptions=["lic-1"]),
        )
        self.assertEqual(
            monitor.subscribed_users(users, info, want=lambda state: state == "active"),
            [("111", "111")],
        )

    def test_online_filter_accepts_every_follower_state(self):
        info = make_license(
            license_id="lic-1", users={"111": {"alert_state": "ignored"}}
        )
        users = self._users(make_user("111", subscriptions=["lic-1"]))
        self.assertEqual(monitor.subscribed_users(users, info), [("111", "111")])

    def test_follower_without_chat_id_is_skipped(self):
        info = make_license(license_id="lic-1")
        entry = make_user("111", subscriptions=["lic-1"])
        entry["chat_id"] = ""
        entry["user_id"] = ""
        users = self._users(entry)
        self.assertEqual(monitor.subscribed_users(users, info), [])


class SubscribeListTests(unittest.TestCase):
    def test_empty_holder_returns_placeholder(self):
        text, keyboard, page, total = monitor.build_subscribe_list(
            make_holder({}), {"users": {}}, "111"
        )
        self.assertIsNone(keyboard)
        self.assertIn("No licenses tracked yet", text)
        self.assertEqual((page, total), (1, 1))

    def test_rows_carry_sub_action_with_page(self):
        holder = make_holder({"lic-1": make_license("lic-1", "Pixel 8")})
        users = {"users": {"111": make_user("111", subscriptions=["lic-1"])}}
        text, keyboard, page, total = monitor.build_subscribe_list(holder, users, "111")
        self.assertEqual((page, total), (1, 1))
        row = keyboard["inline_keyboard"][0][0]
        self.assertTrue(row["callback_data"].startswith("sub:"))
        self.assertTrue(row["callback_data"].endswith(":1"))
        self.assertIn("🟢", row["text"])
        self.assertIn("Pixel 8", text)

    def test_unfollowed_license_shows_hollow_marker(self):
        holder = make_holder({"lic-1": make_license("lic-1", "Pixel 8")})
        users = {"users": {"111": make_user("111", subscriptions=[])}}
        _, keyboard, _, _ = monitor.build_subscribe_list(holder, users, "111")
        self.assertIn("⚪", keyboard["inline_keyboard"][0][0]["text"])

    def test_pagination_adds_nav_row_and_clamps_page(self):
        records = {
            f"lic-{index:03d}": make_license(f"lic-{index:03d}", f"Phone {index}")
            for index in range(monitor.SUBSCRIBE_PAGE_SIZE + 10)
        }
        holder = make_holder(records)
        users = {"users": {"111": make_user("111")}}

        _, keyboard, page, total = monitor.build_subscribe_list(holder, users, "111", 1)
        self.assertEqual(total, 2)
        self.assertEqual(page, 1)
        nav = keyboard["inline_keyboard"][-1]
        self.assertEqual(len(nav), 1)
        self.assertEqual(nav[0]["callback_data"], "page:2")

        _, keyboard, page, _ = monitor.build_subscribe_list(holder, users, "111", 99)
        self.assertEqual(page, 2)
        nav = keyboard["inline_keyboard"][-1]
        self.assertEqual(nav[0]["callback_data"], "page:1")

        rows = [
            row
            for row in keyboard["inline_keyboard"]
            if row[0]["callback_data"].startswith("sub:")
        ]
        self.assertEqual(len(rows), monitor.SUBSCRIBE_PAGE_SIZE + 10 - monitor.SUBSCRIBE_PAGE_SIZE)


class UsersFileTests(unittest.TestCase):
    def setUp(self):
        self._original = monitor.USERS_PATH

    def tearDown(self):
        monitor.USERS_PATH = self._original

    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            monitor.USERS_PATH = Path(tmp) / "users.json"
            monitor.save_users({"users": {"111": make_user("111", subscriptions=["lic-1"])}})
            loaded = monitor.load_users()
            self.assertEqual(loaded["users"]["111"]["subscriptions"], ["lic-1"])
            self.assertTrue((Path(tmp) / "users.json").is_file())
            self.assertFalse((Path(tmp) / "users.json.tmp").exists())

    def test_missing_file_yields_no_users(self):
        with tempfile.TemporaryDirectory() as tmp:
            monitor.USERS_PATH = Path(tmp) / "absent.json"
            self.assertEqual(monitor.load_users(), {"users": {}})

    def test_corrupt_file_yields_no_users(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "users.json"
            path.write_text("{not json", encoding="utf-8")
            monitor.USERS_PATH = path
            self.assertEqual(monitor.load_users(), {"users": {}})

    def test_wrong_shape_yields_no_users(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "users.json"
            path.write_text('{"users": []}', encoding="utf-8")
            monitor.USERS_PATH = path
            self.assertEqual(monitor.load_users(), {"users": {}})


if __name__ == "__main__":
    unittest.main()
