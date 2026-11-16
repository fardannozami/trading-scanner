import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TELEGRAM_BOT_USERNAME = os.getenv("TELEGRAM_BOT_USERNAME", "").lstrip("@")
_alert_channels_value = os.getenv("ALERT_CHANNELS", "telegram").strip().lower()
ALERT_CHANNELS = (
    ("telegram", "whatsapp")
    if _alert_channels_value == "both"
    else tuple(channel.strip() for channel in _alert_channels_value.split(",") if channel.strip())
)
WHATSAPP_SERVICE_URL = os.getenv("WHATSAPP_SERVICE_URL", "http://127.0.0.1:8787").rstrip("/")
WHATSAPP_SERVICE_TOKEN = os.getenv("WHATSAPP_SERVICE_TOKEN", "")
WHATSAPP_RECIPIENT_JID = os.getenv("WHATSAPP_RECIPIENT_JID", "")
WHATSAPP_PHONE = os.getenv("WHATSAPP_PHONE", "")
WHATSAPP_SESSION_DB = os.getenv("WHATSAPP_SESSION_DB", "whatsmeow.db")
WHATSAPP_LISTEN_ADDR = os.getenv("WHATSAPP_LISTEN_ADDR", "127.0.0.1:8787")
TELEGRAM_PAIR_CODE_TTL_MINUTES = int(os.getenv("TELEGRAM_PAIR_CODE_TTL_MINUTES", "10"))
TELEGRAM_PAIR_MAX_ATTEMPTS = int(os.getenv("TELEGRAM_PAIR_MAX_ATTEMPTS", "5"))

if not ALERT_CHANNELS or any(c not in {"telegram", "whatsapp"} for c in ALERT_CHANNELS):
    raise ValueError("ALERT_CHANNELS must be telegram, whatsapp, or telegram,whatsapp")
if len(set(ALERT_CHANNELS)) != len(ALERT_CHANNELS):
    raise ValueError("ALERT_CHANNELS contains duplicate channels")

NOTIFICATION_CHANNELS = ALERT_CHANNELS


def channel_enabled(channel):
    return channel in NOTIFICATION_CHANNELS


def validate_whatsapp_configuration():
    if not channel_enabled("whatsapp"):
        return
    if not WHATSAPP_SERVICE_TOKEN:
        raise ValueError("WHATSAPP_SERVICE_TOKEN is required when WhatsApp notifications are enabled")
    if not WHATSAPP_RECIPIENT_JID:
        raise ValueError("WHATSAPP_RECIPIENT_JID is required when WhatsApp notifications are enabled")


validate_whatsapp_configuration()

MT5_LOGIN = int(os.getenv("MT5_LOGIN", "0"))
MT5_PASSWORD = os.getenv("MT5_PASSWORD", "")
MT5_SERVER = os.getenv("MT5_SERVER", "")

SYMBOLS = ["XAUUSD"]

SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "60"))

MAX_RISK_PER_TRADE = float(os.getenv("MAX_RISK_PER_TRADE", "1.0"))
MAX_DAILY_DRAWDOWN = float(os.getenv("MAX_DAILY_DRAWDOWN", "3.0"))
MAX_WEEKLY_DRAWDOWN = float(os.getenv("MAX_WEEKLY_DRAWDOWN", "6.0"))
MAX_CONSECUTIVE_LOSSES = int(os.getenv("MAX_CONSECUTIVE_LOSSES", "3"))

SCORE_STANDARD = 4
SCORE_A = 7
SCORE_A_PLUS = 10
SCORE_ELITE = 12

HTF = "H4"
MTF = "H1"
LTF = "M15"

DB_PATH = os.getenv("DB_PATH", "trading_scanner.db")
SCREENSHOTS_DIR = "screenshots"
ALERT_COOLDOWN_MINUTES = int(os.getenv("ALERT_COOLDOWN_MINUTES", "60"))

# News filter
NEWS_FILTER_ENABLED = os.getenv("NEWS_FILTER_ENABLED", "true").lower() == "true"
NEWS_BLOCK_MINUTES_BEFORE = int(os.getenv("NEWS_BLOCK_MINUTES_BEFORE", "30"))
NEWS_BLOCK_MINUTES_AFTER = int(os.getenv("NEWS_BLOCK_MINUTES_AFTER", "15"))
# Send a Telegram heads-up this many minutes before a high-impact event that
# affects a tracked symbol. 0 disables the reminder.
NEWS_ALERT_MINUTES_BEFORE = int(os.getenv("NEWS_ALERT_MINUTES_BEFORE", "60"))

# Paper trading (virtual positions used to measure strategy win rate)
PAPER_TRADING_ENABLED = os.getenv("PAPER_TRADING_ENABLED", "true").lower() == "true"
PAPER_LOT = float(os.getenv("PAPER_LOT", "0.01"))
PAPER_TP_LEVEL = os.getenv("PAPER_TP_LEVEL", "tp1").lower()  # "tp1" or "tp2"
PAPER_MAX_HOLD_HOURS = int(os.getenv("PAPER_MAX_HOLD_HOURS", "168"))
# When false, a new signal is ignored while a position on the same symbol is
# still open (per-strategy stats are still refreshed on every signal).
PAPER_ALLOW_STACKING = os.getenv("PAPER_ALLOW_STACKING", "false").lower() == "true"

# ─────────────────────────────────────────────
# Live trading — REAL orders sent to MetaTrader 5.
# Firing real orders needs three explicit opt-ins, so a single copy/paste of
# an example .env can never accidentally trade a real account:
#   1. LIVE_TRADING_ENABLED=true
#   2. LIVE_TRADING_DRY_RUN=false
#   3. LIVE_CONFIRMATION=YES
# Anything less and the bot only validates and logs the intended order.
# ─────────────────────────────────────────────
LIVE_TRADING_ENABLED = os.getenv("LIVE_TRADING_ENABLED", "false").lower() == "true"
LIVE_TRADING_DRY_RUN = os.getenv("LIVE_TRADING_DRY_RUN", "true").lower() == "true"
LIVE_CONFIRMATION = os.getenv("LIVE_CONFIRMATION", "").strip().upper()
LIVE_SEND_ORDERS = (
    LIVE_TRADING_ENABLED
    and not LIVE_TRADING_DRY_RUN
    and LIVE_CONFIRMATION == "YES"
)

# Minimum setup grade that is allowed to become a real order.
LIVE_MIN_GRADE = os.getenv("LIVE_MIN_GRADE", "A+")  # Standard / A / A+ / ELITE
# "fixed" uses LIVE_FIXED_LOT; "risk" sizes from MAX_RISK_PER_TRADE.
LIVE_LOT_MODE = os.getenv("LIVE_LOT_MODE", "risk").lower()
LIVE_FIXED_LOT = float(os.getenv("LIVE_FIXED_LOT", "0.01"))
LIVE_TP_LEVEL = os.getenv("LIVE_TP_LEVEL", "tp1").lower()  # "tp1" or "tp2"
LIVE_MAGIC = int(os.getenv("LIVE_MAGIC", "20261116"))
LIVE_DEVIATION = int(os.getenv("LIVE_DEVIATION", "20"))  # max slippage, points
# Reject entries when the spread is wider than this (0 disables the check).
LIVE_MAX_SPREAD_POINTS = float(os.getenv("LIVE_MAX_SPREAD_POINTS", "50"))
LIVE_MAX_OPEN_POSITIONS = int(os.getenv("LIVE_MAX_OPEN_POSITIONS", "1"))
LIVE_MAX_DAILY_TRADES = int(os.getenv("LIVE_MAX_DAILY_TRADES", "5"))
LIVE_COMMENT = os.getenv("LIVE_COMMENT", "Scanner")
