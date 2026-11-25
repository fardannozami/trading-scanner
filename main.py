import time
import logging
from datetime import datetime, timezone
from html import escape

import config
from scanner.mt5_connector import (
    connect,
    disconnect,
    get_bars,
    get_account_info,
    get_symbol_info,
)
from scanner.market_analyzer import analyze_symbol
from scanner.scoring_engine import score_setup
from scanner.chart_generator import generate_chart
from scanner.news_filter import (
    is_news_blackout,
    fetch_calendar,
    get_upcoming_symbol_events,
)
from alerts.telegram_bot import (
    send_alert,
    send_live_close,
    send_message,
    send_paper_close,
    send_risk_alert,
    send_startup_message,
)
from paper.paper_trader import monitor_open_trades, open_from_signal
from execution.live_trader import open_live_trade, sync_live_trades
from risk.risk_manager import RiskManager
from database.db_manager import (
    init_db,
    save_setup,
    log_risk,
    get_alert_cooldown,
    set_alert_cooldown,
    clear_alert_cooldowns,
    get_open_live_trades,
    get_open_paper_trades,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("scanner.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

_alerted: dict[str, datetime] = {}
_digits_cache: dict[str, int] = {}

GRADE_RANK = {"Standard": 0, "A": 1, "A+": 2, "ELITE": 3}


def _cooldown_ok(key: str, minutes: int | None = None) -> bool:
    """Return True when an alert for `key` is outside its cooldown window.

    Cooldowns are persisted in SQLite so restarting the scanner does not
    re-send alerts that are still cooling down.
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    window = config.ALERT_COOLDOWN_MINUTES if minutes is None else minutes
    try:
        last = get_alert_cooldown(key)
    except Exception:
        log.exception("Could not read cooldown for %s; falling back to memory", key)
        last = _alerted.get(key)

    if last and (now - last).total_seconds() < window * 60:
        return False

    _mark_cooldown(key, now)
    return True


def _cooldown_active(key: str, minutes: int | None = None) -> bool:
    """Like ``not _cooldown_ok`` but read-only: the window is not consumed."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    window = config.ALERT_COOLDOWN_MINUTES if minutes is None else minutes
    try:
        last = get_alert_cooldown(key)
    except Exception:
        log.exception("Could not read cooldown for %s; falling back to memory", key)
        last = _alerted.get(key)
    return bool(last and (now - last).total_seconds() < window * 60)


def _mark_cooldown(key: str, when: datetime | None = None):
    when = when or datetime.now(timezone.utc).replace(tzinfo=None)
    _alerted[key] = when
    try:
        set_alert_cooldown(key, when)
    except Exception:
        log.exception("Could not persist cooldown for %s", key)


def _notify_upcoming_news():
    """Send one Telegram heads-up per upcoming high-impact event per symbol."""
    if not config.NEWS_FILTER_ENABLED or config.NEWS_ALERT_MINUTES_BEFORE <= 0:
        return

    for symbol in config.SYMBOLS:
        try:
            events = get_upcoming_symbol_events(
                symbol, minutes=config.NEWS_ALERT_MINUTES_BEFORE
            )
        except Exception:
            log.exception("Could not read upcoming news for %s", symbol)
            continue

        for ev in events:
            key = "NEWS_UPCOMING|{}|{}|{}".format(
                symbol, ev["datetime_utc"].isoformat(), ev["title"]
            )
            # 12h cooldown: the key is unique per event, so each event is
            # announced exactly once even across restarts.
            if not _cooldown_ok(key, minutes=12 * 60):
                continue
            log.info(
                "%s: upcoming %s news in %sm — %s",
                symbol, ev["currency"], ev["minutes_away"], ev["title"],
            )
            send_message(
                f"⏰ <b>{escape(str(symbol))}</b> news in "
                f"<b>{ev['minutes_away']}m</b>\n"
                f"{escape(str(ev['currency']))} — {escape(str(ev['title']))} "
                f"(<b>{escape(str(ev['impact']))}</b> impact)\n"
                f"Scanner pauses {config.NEWS_BLOCK_MINUTES_BEFORE}m before / "
                f"{config.NEWS_BLOCK_MINUTES_AFTER}m after."
            )


def _symbol_digits(symbol):
    """Broker price digits, cached; None lets the analyzer infer them."""
    if symbol in _digits_cache:
        return _digits_cache[symbol]
    info = get_symbol_info(symbol) or {}
    digits = info.get("digits")
    if digits is not None:
        _digits_cache[symbol] = int(digits)
    return _digits_cache.get(symbol)


def _rearm_symbol(symbol):
    """Clear cooldowns so a closed position unblocks the next alert at once."""
    if not symbol:
        return
    prefix = f"{symbol}_"
    for key in [k for k in _alerted if k.startswith(prefix)]:
        _alerted.pop(key, None)
    try:
        clear_alert_cooldowns(symbol)
    except Exception:
        log.exception("Could not clear alert cooldowns for %s", symbol)


def _position_direction(analysis):
    bias = analysis.get("bias")
    if bias == "bullish":
        return "buy"
    if bias == "bearish":
        return "sell"
    return None


def _open_position(symbol):
    """The open paper or live position for `symbol`, if there is one."""
    try:
        positions = get_open_paper_trades() + get_open_live_trades()
    except Exception:
        log.exception("Could not read open positions for %s", symbol)
        return None
    return next((p for p in positions if p.get("symbol") == symbol), None)


def _alert_allowed(open_position, analysis, scoring):
    """Should a new entry alert fire while `open_position` is still open?

    Duplicate entries in the same direction are suppressed unless the grade
    improves (A → A+ → ELITE). A reversal means the previous setup is no longer
    valid, so that alert is allowed.
    """
    direction = _position_direction(analysis)
    if direction and direction != open_position.get("direction"):
        return True
    return GRADE_RANK.get(scoring.get("grade"), -1) > GRADE_RANK.get(
        open_position.get("grade"), -1
    )


def _monitor_paper_trades():
    try:
        for event in monitor_open_trades():
            trade = event["trade"]
            stats = event.get("stats") or {}
            log.info(
                "Paper trade #%s closed %s (%+.2fR) — %s win rate %.1f%%",
                trade["id"], trade["exit_reason"],
                trade.get("r_multiple") or 0.0,
                trade["strategy"], stats.get("win_rate", 0.0),
            )
            send_paper_close(event)
            _rearm_symbol(trade.get("symbol"))
    except Exception:
        log.exception("Paper trade monitor failed")


def _sync_live_trades():
    try:
        for event in sync_live_trades():
            trade = event["trade"]
            log.warning(
                "Live trade #%s closed: %s %s (%+.2f)",
                trade["id"], trade["symbol"], trade.get("status"),
                event.get("profit") or 0.0,
            )
            send_live_close(event)
            _rearm_symbol(trade.get("symbol"))
    except Exception:
        log.exception("Live trade sync failed")


def determine_stage(scoring, analysis) -> str:
    """Alert stage; never lower for a higher grade (A+ >= A)."""
    grade = scoring.get("grade")
    conditions = analysis.get("conditions", [])
    if grade == "ELITE":
        return "high_confidence"
    if grade == "A+" and "Rejection Candle" in conditions:
        return "m15_confirmation"
    if grade in ("A", "A+"):
        return "entry_zone"
    return "setup_developing"


def _alert_cooldown(symbol, analysis, grade, position):
    """(cooldown key, window in minutes) for an entry alert.

    The key includes the direction so a reversal is not blocked by the
    previous opposite-side alert. While a position is open, an allowed
    upgrade/reversal alert is keyed to that position and lasts until the
    position closes (``_rearm_symbol`` clears every ``SYMBOL_*`` key), so it
    is sent once instead of every cooldown window.
    """
    direction = _position_direction(analysis) or "flat"
    key = f"{symbol}_{direction}_{grade}"
    if position is None:
        return key, None
    position_ref = position.get("opened_at") or position.get("id")
    return f"{key}_pos_{position_ref}", 365 * 24 * 60


def scan_once(risk_manager: RiskManager):
    # Heads-up for high-impact news before anything else.
    _notify_upcoming_news()

    # Track virtual positions even if risk rules currently block new alerts.
    _monitor_paper_trades()

    # Reconcile real positions with the broker (SL/TP hits, manual closes).
    _sync_live_trades()

    account = get_account_info()
    if not account:
        log.warning("Could not fetch account info from MT5")
        return

    balance = account.get("balance", 0)
    equity = account.get("equity", balance)
    risk_status = risk_manager.check_risk(
        balance, equity,
        login=account.get("login"),
        currency=account.get("currency"),
    )
    try:
        log_risk(
            balance, equity, risk_status,
            login=account.get("login"),
            currency=account.get("currency"),
        )
    except Exception:
        # A transient DB failure must not block the scan.
        log.exception("Could not write risk log")

    if not risk_status["allowed"]:
        for v in risk_status["violations"]:
            log.warning(f"Risk violation: {v}")
        if _cooldown_ok("RISK_VIOLATION"):
            send_risk_alert(risk_status["violations"], risk_status["warnings"])
        return

    for sym in risk_status.get("warnings", []):
        log.info(f"Risk warning: {sym}")

    for symbol in config.SYMBOLS:
        try:
            htf_df = get_bars(symbol, config.HTF, 200)
            mtf_df = get_bars(symbol, config.MTF, 200)
            ltf_df = get_bars(symbol, config.LTF, 100)

            if htf_df is None or mtf_df is None or ltf_df is None:
                log.warning(f"{symbol}: failed to fetch bars")
                continue

            analysis = analyze_symbol(
                htf_df, mtf_df, ltf_df, symbol,
                digits=_symbol_digits(symbol),
            )
            scoring = score_setup(analysis)
            grade = scoring.get("grade")
            score = scoring.get("score", 0)

            log.info(
                f"{symbol}: bias={analysis['bias']} score={score} grade={grade} "
                f"conditions={analysis['conditions']}"
            )

            if grade is None:
                continue

            if not analysis.get("levels"):
                # No tradeable entry/SL/TP (e.g. stop > MAX_SL_ATR x ATR).
                log.info(f"{symbol}: {grade} skipped — no valid entry levels "
                         f"(stop too far or levels invalid)")
                continue

            # News blackout check
            if config.NEWS_FILTER_ENABLED:
                blackout, reason = is_news_blackout(symbol)
                if blackout:
                    log.info(f"{symbol}: skipped — news blackout ({reason})")
                    if _cooldown_ok(f"NEWS_{symbol}"):
                        send_message(
                            f"🚫 <b>{escape(str(symbol))}</b> alert suppressed\n"
                            f"<i>{escape(str(reason))}</i>\n"
                            f"Window: -{config.NEWS_BLOCK_MINUTES_BEFORE}m / +{config.NEWS_BLOCK_MINUTES_AFTER}m"
                        )
                    continue

            # One entry alert at a time per symbol: while a position in the
            # same direction is open, only a grade upgrade re-alerts.
            position = _open_position(symbol)
            if position and not _alert_allowed(position, analysis, scoring):
                log.info(
                    f"{symbol}: {grade} alert suppressed — "
                    f"{position.get('direction')} position open "
                    f"(grade {position.get('grade')})"
                )
                continue

            cooldown_key, window = _alert_cooldown(symbol, analysis, grade, position)
            if _cooldown_active(cooldown_key, window):
                log.info(f"{symbol}: {grade} alert in cooldown, skipping")
                continue

            # The window is only consumed once the chart exists: if chart
            # generation fails nothing was recorded, so the next scan retries.
            chart_path = generate_chart(symbol, ltf_df, analysis, scoring, config.LTF)
            _mark_cooldown(cooldown_key)
            setup_id = save_setup(symbol, config.LTF, analysis, scoring, chart_path)
            paper_trade = None
            try:
                paper_trade = open_from_signal(
                    symbol, analysis, scoring,
                    setup_id=setup_id, timeframe=config.LTF, df=ltf_df,
                )
            except Exception:
                # Never let paper-trade bookkeeping block a real alert.
                log.exception("Paper trade open failed for %s", symbol)

            live_trade = None
            if config.LIVE_TRADING_ENABLED:
                try:
                    live_trade = open_live_trade(
                        symbol, analysis, scoring, risk_manager,
                        setup_id=setup_id, timeframe=config.LTF,
                    )
                except Exception:
                    # Execution errors must not crash the scan loop.
                    log.exception("Live trade execution failed for %s", symbol)

            stage = determine_stage(scoring, analysis)
            sent = send_alert(
                symbol, analysis, scoring, chart_path, stage,
                paper_trade=paper_trade, live_trade=live_trade,
            )
            if not sent:
                # Retry once as plain text without the chart before giving up.
                sent = send_alert(
                    symbol, analysis, scoring, None, stage,
                    paper_trade=paper_trade, live_trade=live_trade,
                )
            if sent:
                log.info(f"Alert sent — {symbol} {grade} (score {score}) stage={stage}")
            else:
                log.error(
                    f"Alert delivery FAILED — {symbol} {grade} (score {score}); "
                    "setup and paper trade were still recorded"
                )

        except Exception:
            log.exception(f"Error scanning {symbol}")


def main():
    log.info("Trading Scanner initialising...")
    init_db()

    if config.LIVE_TRADING_ENABLED:
        if config.LIVE_SEND_ORDERS:
            log.warning(
                "LIVE TRADING ACTIVE — real orders will be sent to MT5 "
                "(min grade %s, %s lot mode)",
                config.LIVE_MIN_GRADE, config.LIVE_LOT_MODE,
            )
        else:
            log.warning(
                "Live trading enabled but NOT armed (dry-run=%s, confirmation=%s) "
                "— orders will only be logged",
                config.LIVE_TRADING_DRY_RUN, "ok" if config.LIVE_CONFIRMATION == "YES" else "missing",
            )

    try:
        connect()
        log.info("Connected to MetaTrader 5")
    except RuntimeError as e:
        log.error(str(e))
        return

    # Pre-warm the calendar cache on startup
    if config.NEWS_FILTER_ENABLED:
        events = fetch_calendar()
        if events:
            log.info("Economic calendar loaded (%d events)", len(events))
        else:
            log.warning(
                "Economic calendar unavailable — news filter has no events"
            )

    send_startup_message(config.SYMBOLS)
    risk_manager = RiskManager()

    try:
        while True:
            try:
                scan_once(risk_manager)
            except Exception:
                # Keep the scanner alive through transient MT5/DB failures.
                log.exception("Scan cycle failed — continuing")
            log.info(f"Scan complete — next in {config.SCAN_INTERVAL}s")
            time.sleep(config.SCAN_INTERVAL)
    except KeyboardInterrupt:
        log.info("Scanner stopped")
        send_message("⏹ <b>Trading Scanner stopped.</b>")
    finally:
        disconnect()


if __name__ == "__main__":
    main()
