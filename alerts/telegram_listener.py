import hashlib
import logging
import time

import requests

import config
from database.db_manager import (
    consume_telegram_pair_code,
    get_notification_state,
    increment_pair_attempt,
    init_db,
    set_notification_state,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)


class TelegramPairListener:
    def __init__(self):
        self.base_url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}"
        self.offset = int(get_notification_state("telegram_update_offset") or 0)

    def _api(self, method, **kwargs):
        response = requests.post(
            f"{self.base_url}/{method}", json=kwargs, timeout=40
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(f"Telegram {method} returned ok=false")
        return payload.get("result")

    def _is_group_admin(self, chat_id, user_id):
        member = self._api(
            "getChatMember", chat_id=chat_id, user_id=user_id
        )
        return member.get("status") in {"creator", "administrator"}

    def _handle_message(self, message):
        text = str(message.get("text") or "").strip()
        parts = text.split()
        if not parts or parts[0].split("@", 1)[0] != "/pair":
            return

        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        chat_id = str(chat.get("id", ""))
        user_id = sender.get("id")
        chat_type = chat.get("type", "")
        if not chat_id or user_id is None or chat_type not in {"private", "group", "supergroup"}:
            return

        if chat_type != "private":
            try:
                if not self._is_group_admin(chat_id, user_id):
                    self._api(
                        "sendMessage", chat_id=chat_id,
                        text="Pairing group hanya boleh dilakukan admin grup.",
                    )
                    return
            except Exception:
                log.exception("Could not verify group admin for pairing")
                return

        if len(parts) != 2 or not parts[1].isdigit() or len(parts[1]) != 8:
            self._api(
                "sendMessage", chat_id=chat_id,
                text="Format pairing: /pair KODE (kode 8 digit berlaku sementara).",
            )
            return

        code_hash = hashlib.sha256(parts[1].encode("ascii")).hexdigest()
        paired = consume_telegram_pair_code(
            code_hash,
            chat_id,
            "private" if chat_type == "private" else "group",
            user_id,
            max_attempts=config.TELEGRAM_PAIR_MAX_ATTEMPTS,
        )
        if paired:
            self._api(
                "sendMessage", chat_id=chat_id,
                text="Pairing berhasil. Chat ini akan menerima notifikasi scanner.",
            )
            log.info("Telegram notification target paired (%s)", chat_type)
        else:
            increment_pair_attempt(
                code_hash, max_attempts=config.TELEGRAM_PAIR_MAX_ATTEMPTS
            )
            self._api(
                "sendMessage", chat_id=chat_id,
                text="Kode tidak valid, sudah dipakai, atau kedaluwarsa.",
            )

    def run(self):
        if not config.TELEGRAM_BOT_TOKEN:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is required for pairing listener")
        while True:
            try:
                updates = self._api(
                    "getUpdates", offset=self.offset, timeout=30,
                    allowed_updates=["message"],
                ) or []
                for update in updates:
                    self.offset = max(self.offset, int(update["update_id"]) + 1)
                    try:
                        message = update.get("message") or {}
                        if message:
                            self._handle_message(message)
                    except Exception:
                        log.exception("Failed to process Telegram update")
                    finally:
                        set_notification_state(
                            "telegram_update_offset", str(self.offset)
                        )
            except KeyboardInterrupt:
                raise
            except Exception:
                log.exception("Telegram pairing listener failed; retrying")
                time.sleep(5)


def main():
    init_db()
    TelegramPairListener().run()


if __name__ == "__main__":
    main()
