import logging
import re
from html import unescape

import requests

import config
from database.db_manager import (
    get_telegram_targets,
    migrate_telegram_target,
    remove_telegram_target,
)

log = logging.getLogger(__name__)
_TAG_RE = re.compile(r"</?(?:b|i|u|s|code|pre|a)(?:\s+[^>]*)?>", re.IGNORECASE)


TELEGRAM_CAPTION_LIMIT = 1024


def _telegram_chat_ids():
    targets = [row["chat_id"] for row in get_telegram_targets()]
    if not targets and config.TELEGRAM_CHAT_ID:
        targets = [str(config.TELEGRAM_CHAT_ID)]
    return list(dict.fromkeys(targets))


def _handle_dead_target(chat_id, response):
    """Deactivate or migrate a paired chat Telegram says is gone.

    Without this a kicked bot (403), a deleted chat ("chat not found") or a
    group upgraded to a supergroup keeps failing forever, and because the
    paired list is non-empty the TELEGRAM_CHAT_ID fallback is never used.
    The legacy TELEGRAM_CHAT_ID itself is never touched.
    """
    try:
        body = response.json()
    except ValueError:
        return
    paired = {row["chat_id"] for row in get_telegram_targets()}
    if str(chat_id) not in paired:
        return
    new_chat_id = (body.get("parameters") or {}).get("migrate_to_chat_id")
    description = str(body.get("description") or "").lower()
    if new_chat_id:
        migrate_telegram_target(chat_id, new_chat_id)
        log.warning("Telegram chat %s migrated to %s; target updated", chat_id, new_chat_id)
    elif response.status_code == 403 or "chat not found" in description:
        remove_telegram_target(chat_id)
        log.warning(
            "Telegram chat %s unreachable (%s); paired target deactivated",
            chat_id, body.get("description"),
        )


def _post_telegram(method, chat_id, **kwargs):
    response = requests.post(
        f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/{method}", **kwargs
    )
    if response.status_code in (400, 403):
        _handle_dead_target(chat_id, response)
    response.raise_for_status()
    if not response.json().get("ok"):
        raise RuntimeError("Telegram API returned ok=false")


def _send_telegram_text(text, parse_mode="HTML"):
    if not config.TELEGRAM_BOT_TOKEN:
        log.warning("Telegram notification skipped: bot token is not configured")
        return False
    chat_ids = _telegram_chat_ids()
    if not chat_ids:
        log.warning("Telegram notification skipped: no paired chat or legacy chat ID")
        return False
    ok = True
    for chat_id in chat_ids:
        try:
            _post_telegram(
                "sendMessage", chat_id,
                json={"chat_id": chat_id, "text": text, "parse_mode": parse_mode},
                timeout=15,
            )
        except Exception as exc:
            ok = False
            log.warning("Telegram delivery failed (%s)", type(exc).__name__)
    return ok


def _send_telegram_photo(photo_path, caption=""):
    if not config.TELEGRAM_BOT_TOKEN:
        log.warning("Telegram photo skipped: bot token is not configured")
        return False
    chat_ids = _telegram_chat_ids()
    if not chat_ids:
        log.warning("Telegram photo skipped: no paired chat or legacy chat ID")
        return False
    # Cutting HTML at 1024 chars can split a tag/entity and Telegram then
    # rejects the whole photo. Send a long caption as a separate message.
    overflow = len(caption) > TELEGRAM_CAPTION_LIMIT
    photo_caption = "" if overflow else caption
    ok = True
    for chat_id in chat_ids:
        try:
            with open(photo_path, "rb") as photo:
                _post_telegram(
                    "sendPhoto", chat_id,
                    data={"chat_id": chat_id, "caption": photo_caption, "parse_mode": "HTML"},
                    files={"photo": photo},
                    timeout=30,
                )
            if overflow:
                _post_telegram(
                    "sendMessage", chat_id,
                    json={"chat_id": chat_id, "text": caption, "parse_mode": "HTML"},
                    timeout=15,
                )
        except Exception as exc:
            ok = False
            log.warning("Telegram photo delivery failed (%s)", type(exc).__name__)
    return ok


def _send_whatsapp(text):
    if not config.WHATSAPP_RECIPIENT_JID:
        log.warning("WhatsApp notification skipped: WHATSAPP_RECIPIENT_JID is empty")
        return False
    try:
        response = requests.post(
            f"{config.WHATSAPP_SERVICE_URL}/send",
            headers={"Authorization": f"Bearer {config.WHATSAPP_SERVICE_TOKEN}"},
            json={"jid": config.WHATSAPP_RECIPIENT_JID, "text": unescape(_TAG_RE.sub("", text))},
            timeout=20,
        )
        response.raise_for_status()
        return True
    except Exception as exc:
        log.warning("WhatsApp notification delivery failed (%s)", type(exc).__name__)
        return False


def _try_delivery(channel, sender, *args):
    try:
        return bool(sender(*args))
    except Exception as exc:
        log.warning("Notification delivery failed for %s (%s)", channel, type(exc).__name__)
        return False


def send_text(text, parse_mode="HTML"):
    results = []
    for channel in config.NOTIFICATION_CHANNELS:
        if channel == "telegram":
            results.append(_try_delivery(channel, _send_telegram_text, text, parse_mode))
        elif channel == "whatsapp":
            results.append(_try_delivery(channel, _send_whatsapp, text))
    return any(results)


def send_photo(photo_path, caption=""):
    results = []
    for channel in config.NOTIFICATION_CHANNELS:
        if channel == "telegram":
            results.append(_try_delivery(channel, _send_telegram_photo, photo_path, caption))
        elif channel == "whatsapp":
            results.append(_try_delivery(
                channel, _send_whatsapp,
                caption or "Trading scanner update (chart not attached).",
            ))
    return any(results)
