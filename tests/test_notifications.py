import hashlib
import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "notifications.db")
os.environ["ALERT_CHANNELS"] = "telegram"

import config
import database.db_manager as db
from alerts.pairing import hash_pair_code, issue_pair_code
from alerts.telegram_listener import TelegramPairListener
import alerts.dispatcher as dispatcher


def _timestamp(delta=timedelta()):
    return (datetime.now(timezone.utc) + delta).replace(tzinfo=None).isoformat(
        sep=" ", timespec="seconds"
    )


class NotificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init_db()

    def test_pair_telegram_group_once_and_rate_limit_attempts(self):
        code, expires = issue_pair_code()
        self.assertEqual(len(code), 8)
        self.assertGreater(expires, datetime.now(timezone.utc))

        listener = TelegramPairListener()
        listener._api = Mock(return_value={"status": "administrator"})
        listener._handle_message({
            "text": f"/pair {code}",
            "chat": {"id": -100123, "type": "supergroup"},
            "from": {"id": 987},
        })
        targets = db.get_telegram_targets()
        paired_target = next(row for row in targets if row["chat_id"] == "-100123")
        self.assertEqual(paired_target["chat_type"], "group")
        self.assertFalse(db.consume_telegram_pair_code(
            hash_pair_code(code), "-100456", "group", "100"
        ))

        subject = "telegram:private:42"
        for _ in range(config.TELEGRAM_PAIR_MAX_ATTEMPTS):
            self.assertTrue(db.register_pair_attempt(
                subject, max_attempts=config.TELEGRAM_PAIR_MAX_ATTEMPTS
            ))
        self.assertFalse(db.register_pair_attempt(
            subject, max_attempts=config.TELEGRAM_PAIR_MAX_ATTEMPTS
        ))

    def test_pair_code_is_expired_and_only_consumable_once_under_race(self):
        expired = hashlib.sha256(b"expired-test").hexdigest()
        db.create_notification_pair_code(
            expired, "telegram", _timestamp(timedelta(minutes=-1))
        )
        self.assertFalse(db.consume_telegram_pair_code(expired, "1", "private", "1"))

        race_code = hashlib.sha256(b"race-test").hexdigest()
        db.create_notification_pair_code(
            race_code, "telegram", _timestamp(timedelta(minutes=10))
        )
        barrier = threading.Barrier(8)
        results = []

        def consume(index):
            barrier.wait()
            results.append(db.consume_telegram_pair_code(
                race_code, f"-100{index}", "group", str(index)
            ))

        threads = [threading.Thread(target=consume, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sum(results), 1)

    def test_dispatches_both_and_isolates_provider_failure(self):
        config.NOTIFICATION_CHANNELS = ("telegram", "whatsapp")
        with patch.object(dispatcher, "_send_telegram_text", return_value=True) as tg, \
             patch.object(dispatcher, "_send_whatsapp", return_value=True) as wa:
            self.assertTrue(dispatcher.send_text("<b>hello</b>"))
            tg.assert_called_once()
            wa.assert_called_once_with("<b>hello</b>")

        with patch.object(dispatcher, "_send_telegram_text", side_effect=RuntimeError("mock")), \
             patch.object(dispatcher, "_send_whatsapp", return_value=True):
            self.assertTrue(dispatcher.send_text("hello"))

    def test_dispatches_photo_caption_to_whatsapp(self):
        config.NOTIFICATION_CHANNELS = ("telegram", "whatsapp")
        with patch.object(dispatcher, "_send_telegram_photo", return_value=True), \
             patch.object(dispatcher, "_send_whatsapp", return_value=True) as wa:
            self.assertTrue(dispatcher.send_photo("chart.png", "<b>caption</b>"))
            wa.assert_called_once_with("<b>caption</b>")


if __name__ == "__main__":
    unittest.main()
