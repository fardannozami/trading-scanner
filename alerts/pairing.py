import argparse
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

import config
from database.db_manager import (
    create_notification_pair_code,
    get_telegram_targets,
    init_db,
    remove_telegram_target,
)


def _utc_text(value):
    return value.astimezone(timezone.utc).replace(tzinfo=None).isoformat(
        sep=" ", timespec="seconds"
    )


def hash_pair_code(code):
    return hmac.new(
        config.TELEGRAM_BOT_TOKEN.encode("utf-8"),
        code.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()


def issue_pair_code(channel="telegram"):
    channel = channel.lower()
    if channel != "telegram":
        raise ValueError("Telegram pair codes are only for Telegram chats")
    code = f"{secrets.randbelow(100_000_000):08d}"
    expires = datetime.now(timezone.utc) + timedelta(
        minutes=config.TELEGRAM_PAIR_CODE_TTL_MINUTES
    )
    create_notification_pair_code(
        hash_pair_code(code),
        channel,
        _utc_text(expires),
    )
    return code, expires


def main():
    parser = argparse.ArgumentParser(prog="python -m alerts.pairing")
    commands = parser.add_subparsers(dest="command", required=True)
    issue = commands.add_parser("issue")
    issue.add_argument("channel", choices=("telegram",), default="telegram", nargs="?")
    commands.add_parser("list")
    remove = commands.add_parser("remove")
    remove.add_argument("chat_id")
    args = parser.parse_args()
    init_db()

    if args.command == "issue":
        code, expires = issue_pair_code(args.channel)
        print(f"Pair code ({args.channel}): {code}")
        print(f"Expires at UTC: {_utc_text(expires)}")
        if args.channel == "telegram":
            print(f"Send /pair {code} to the bot from the target private chat or group.")
    elif args.command == "list":
        targets = get_telegram_targets()
        if not targets:
            print("No Telegram chats paired.")
        for target in targets:
            print(f"{target['chat_type']} chat_id={target['chat_id']} paired_at={target['paired_at']}")
    elif args.command == "remove":
        print("Removed." if remove_telegram_target(args.chat_id) else "Active Telegram target not found.")


if __name__ == "__main__":
    main()
