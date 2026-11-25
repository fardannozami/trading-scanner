"""Live order execution against MetaTrader 5.

This module turns a scanner signal into a REAL broker order. It is gated by
three independent switches in ``config`` (``LIVE_TRADING_ENABLED``,
``LIVE_TRADING_DRY_RUN`` and ``LIVE_CONFIRMATION``) so nothing fires unless the
operator opts in explicitly. Every accepted, rejected or dry-run order is
recorded in the ``live_trades`` table.
"""

import logging
import math

import config
from database.db_manager import (
    close_live_trade,
    count_live_trades_since,
    create_live_trade,
    get_open_live_trades,
)
from paper.paper_trader import validate_trade
from scanner.mt5_connector import (
    get_position_deals,
    get_positions,
    get_symbol_info,
    get_tick,
    send_order,
)

try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except ImportError:
    MT5_AVAILABLE = False

log = logging.getLogger(__name__)

GRADE_RANK = {"Standard": 1, "A": 2, "A+": 3, "ELITE": 4}

# SYMBOL_FILLING_* flags reported by MT5's symbol_info.filling_mode bitmask.
_FILLING_FOK = 1
_FILLING_IOC = 2


def _direction(analysis):
    bias = analysis.get("bias")
    if bias == "bullish":
        return "buy"
    if bias == "bearish":
        return "sell"
    return None


def _target_price(levels):
    if config.LIVE_TP_LEVEL == "tp2" and levels.get("tp2"):
        return levels["tp2"]
    return levels.get("tp1")


def _grade_ok(grade):
    return GRADE_RANK.get(grade, 0) >= GRADE_RANK.get(config.LIVE_MIN_GRADE, 4)


def _decimals(step):
    text = ("%.10f" % float(step)).rstrip("0")
    return len(text.split(".")[1]) if "." in text else 0


def _normalize_volume(lot, info):
    """Clamp a lot size to the broker's min/max/step."""
    step = float(info.get("volume_step") or 0.01) or 0.01
    vmin = float(info.get("volume_min") or step)
    vmax = float(info.get("volume_max") or 100.0)

    lot = max(vmin, min(float(lot), vmax))
    lot = math.floor(lot / step + 1e-9) * step
    if lot < vmin:
        lot = vmin
    lot = min(lot, vmax)
    return round(lot, _decimals(step))


def _filling_candidates(info):
    """Order of filling modes to try, starting with the broker's preference."""
    mode = int(info.get("filling_mode") or 0)
    preferred = []
    if mode & _FILLING_FOK:
        preferred.append(mt5.ORDER_FILLING_FOK)
    if mode & _FILLING_IOC:
        preferred.append(mt5.ORDER_FILLING_IOC)
    # Always keep the three modes as fallbacks, even when the broker reports
    # no preference, so an INVALID_FILL retcode can still be retried.
    ordered = preferred + [
        mt5.ORDER_FILLING_FOK,
        mt5.ORDER_FILLING_IOC,
        mt5.ORDER_FILLING_RETURN,
    ]
    result = []
    for value in ordered:
        if value not in result:
            result.append(value)
    return result


def _stops_ok(direction, price, sl, tp, info):
    """Broker minimum stop distance (trade_stops_level, in points)."""
    stops = int(info.get("trade_stops_level") or 0)
    point = float(info.get("point") or 0)
    if stops <= 0 or point <= 0:
        return True
    min_distance = stops * point
    return abs(price - sl) >= min_distance and abs(tp - price) >= min_distance


def open_live_trade(symbol, analysis, scoring, risk_manager=None,
                    setup_id=None, timeframe=None):
    """Send a real market order for a signal.

    Returns None when live trading is off or a gate rejects the signal. When an
    order is attempted returns a dict: ``{"ok", "dry_run", "ticket", ...}``.
    """
    if not config.LIVE_TRADING_ENABLED:
        return None

    if not MT5_AVAILABLE:
        log.error("Live trade skipped for %s: MetaTrader5 package unavailable", symbol)
        return None

    levels = analysis.get("levels") or {}
    direction = _direction(analysis)
    if not direction or not levels:
        return None

    entry = levels.get("entry")
    sl = levels.get("sl")
    tp_signal = _target_price(levels)
    if entry is None or sl is None or tp_signal is None:
        log.info("Live trade skipped for %s: incomplete levels", symbol)
        return None

    if not validate_trade(direction, entry, sl, tp_signal):
        log.info(
            "Live trade skipped for %s: invalid signal levels "
            "(direction=%s entry=%s sl=%s tp=%s)",
            symbol, direction, entry, sl, tp_signal,
        )
        return None

    grade = scoring.get("grade")
    if not _grade_ok(grade):
        log.info(
            "Live trade skipped for %s: grade %s below minimum %s",
            symbol, grade, config.LIVE_MIN_GRADE,
        )
        return None

    info = get_symbol_info(symbol)
    if not info:
        log.warning("Live trade skipped for %s: symbol info unavailable", symbol)
        return None

    tick = get_tick(symbol)
    if not tick or not tick.get("ask") or not tick.get("bid"):
        log.warning("Live trade skipped for %s: no tick data", symbol)
        return None

    # Direction availability (disabled / long-only / short-only symbols).
    trade_mode = int(info.get("trade_mode") or 4)
    if trade_mode == 0:
        log.warning("Live trade skipped for %s: trading disabled by broker", symbol)
        return None
    if trade_mode == 1 and direction == "sell":
        log.info("Live trade skipped for %s: symbol is long-only", symbol)
        return None
    if trade_mode == 2 and direction == "buy":
        log.info("Live trade skipped for %s: symbol is short-only", symbol)
        return None

    # Daily trade cap.
    daily = count_live_trades_since()
    if daily >= config.LIVE_MAX_DAILY_TRADES:
        log.info(
            "Live trade skipped for %s: daily cap reached (%d/%d)",
            symbol, daily, config.LIVE_MAX_DAILY_TRADES,
        )
        return None

    # Exposure caps: total open real positions and per-symbol stacking.
    positions = get_positions(magic=config.LIVE_MAGIC)
    if positions is None:
        log.warning("Live trade skipped for %s: could not read open positions", symbol)
        return None
    if len(positions) >= config.LIVE_MAX_OPEN_POSITIONS:
        log.info(
            "Live trade skipped for %s: max open positions reached (%d/%d)",
            symbol, len(positions), config.LIVE_MAX_OPEN_POSITIONS,
        )
        return None
    if any(p["symbol"] == symbol for p in positions):
        log.info("Live trade skipped for %s: position already open", symbol)
        return None

    # Market execution price.
    price = float(tick["ask"] if direction == "buy" else tick["bid"])
    digits = int(info.get("digits") or 5)
    price = round(price, digits)
    sl = round(float(sl), digits)
    tp = round(float(tp_signal), digits)

    if not validate_trade(direction, price, sl, tp):
        log.info(
            "Live trade skipped for %s: price %.5f outside SL/TP "
            "(sl=%s tp=%s)", symbol, price, sl, tp,
        )
        return None

    if not _stops_ok(direction, price, sl, tp, info):
        log.info(
            "Live trade skipped for %s: SL/TP inside broker minimum stop distance",
            symbol,
        )
        return None

    point = float(info.get("point") or 0)
    spread_points = (float(tick["spread"]) / point) if point else 0.0
    if (
        config.LIVE_MAX_SPREAD_POINTS > 0
        and spread_points > config.LIVE_MAX_SPREAD_POINTS
    ):
        log.info(
            "Live trade skipped for %s: spread %.1f pts > limit %.1f",
            symbol, spread_points, config.LIVE_MAX_SPREAD_POINTS,
        )
        return None

    # Position size.
    if config.LIVE_LOT_MODE == "risk" and risk_manager is not None:
        account_balance = _account_balance()
        if account_balance is None:
            log.warning("Live trade skipped for %s: account balance unavailable", symbol)
            return None
        lot = risk_manager.calculate_position_size(
            account_balance, price, sl, symbol=symbol,
        )
        # Never round a risk-sized lot up to the broker minimum: that would
        # risk more than MAX_RISK_PER_TRADE.
        vmin = float(info.get("volume_min") or 0.01)
        if lot < vmin:
            log.info(
                "Live trade skipped for %s: risk lot %.4f below broker minimum %s",
                symbol, lot, vmin,
            )
            return None
    else:
        lot = config.LIVE_FIXED_LOT
    lot = _normalize_volume(lot, info)
    if lot <= 0:
        log.warning("Live trade skipped for %s: computed lot size is 0", symbol)
        return None

    order_type = mt5.ORDER_TYPE_BUY if direction == "buy" else mt5.ORDER_TYPE_SELL
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": lot,
        "type": order_type,
        "price": price,
        "sl": sl,
        "tp": tp,
        "deviation": config.LIVE_DEVIATION,
        "magic": config.LIVE_MAGIC,
        "comment": config.LIVE_COMMENT,
        "type_time": mt5.ORDER_TIME_GTC,
    }

    strategy = scoring.get("grade") or "Standard"

    if not config.LIVE_SEND_ORDERS:
        log.info(
            "[LIVE DRY-RUN] %s %s %.2f lot @ %s (SL %s, TP %s) — order not sent",
            symbol, direction.upper(), lot, price, sl, tp,
        )
        return {
            "ok": False,
            "dry_run": True,
            "ticket": None,
            "symbol": symbol,
            "direction": direction,
            "lot": lot,
            "entry": price,
            "sl": sl,
            "tp": tp,
            "retcode": None,
            "comment": "dry-run: order not sent",
            "grade": grade,
            "strategy": strategy,
        }

    # Try the broker's preferred filling mode, then the alternatives.
    response = None
    for filling in _filling_candidates(info):
        request["type_filling"] = filling
        response = send_order(request)
        if response["ok"]:
            break
        if response["retcode"] != mt5.TRADE_RETCODE_INVALID_FILL:
            break

    result = response["result"] if response else None
    order_ticket = getattr(result, "order", None) or None
    deal_ticket = getattr(result, "deal", None) or None
    filled_price = getattr(result, "price", None) or price

    if response and response["ok"]:
        trade_id = create_live_trade(
            symbol=symbol,
            direction=direction,
            lot=float(getattr(result, "volume", lot) or lot),
            entry=float(filled_price),
            requested_price=price,
            sl=sl,
            tp=tp,
            status="open",
            ticket=order_ticket,
            order_ticket=order_ticket,
            strategy=strategy,
            grade=grade,
            score=scoring.get("score"),
            setup_id=setup_id,
            magic=config.LIVE_MAGIC,
            retcode=response["retcode"],
            comment=str(getattr(result, "comment", "") or ""),
            raw=str(result),
        )
        log.warning(
            "LIVE ORDER FILLED: #%s %s %s %.2f lot @ %s (SL %s, TP %s) retcode=%s",
            trade_id, symbol, direction.upper(), lot, filled_price, sl, tp,
            response["retcode"],
        )
        return {
            "ok": True,
            "dry_run": False,
            "id": trade_id,
            "ticket": order_ticket,
            "deal": deal_ticket,
            "symbol": symbol,
            "direction": direction,
            "lot": lot,
            "entry": filled_price,
            "sl": sl,
            "tp": tp,
            "retcode": response["retcode"],
            "comment": str(getattr(result, "comment", "") or ""),
            "grade": grade,
            "strategy": strategy,
        }

    retcode = response["retcode"] if response else None
    comment = response["comment"] if response else "no response"
    create_live_trade(
        symbol=symbol,
        direction=direction,
        lot=lot,
        entry=price,
        requested_price=price,
        sl=sl,
        tp=tp,
        status="error",
        ticket=None,
        strategy=strategy,
        grade=grade,
        score=scoring.get("score"),
        setup_id=setup_id,
        magic=config.LIVE_MAGIC,
        retcode=retcode,
        comment=str(comment),
        raw=str(result) if result is not None else "",
    )
    log.error(
        "LIVE ORDER REJECTED: %s %s %.2f lot @ %s retcode=%s comment=%s",
        symbol, direction.upper(), lot, price, retcode, comment,
    )
    return {
        "ok": False,
        "dry_run": False,
        "ticket": None,
        "symbol": symbol,
        "direction": direction,
        "lot": lot,
        "entry": price,
        "sl": sl,
        "tp": tp,
        "retcode": retcode,
        "comment": str(comment),
        "grade": grade,
        "strategy": strategy,
    }


def _account_balance():
    """Account balance, or None when it cannot be read (caller must skip)."""
    if not MT5_AVAILABLE:
        return None
    try:
        info = mt5.account_info()
    except Exception:
        return None
    if not info or float(info.balance) <= 0:
        return None
    return float(info.balance)


def sync_live_trades():
    """Reconcile open live_trades rows with MT5 positions.

    A row whose ticket is no longer among the open positions was closed by the
    broker (SL/TP hit or manual close); its deals are summarised to recover the
    exit price and realised profit. Returns a list of close events.
    """
    if not MT5_AVAILABLE:
        return []

    trades = get_open_live_trades()
    if not trades:
        return []

    positions = get_positions(magic=config.LIVE_MAGIC)
    if positions is None:
        # MT5 query failed: an empty answer would wrongly close every row.
        log.warning("Live sync skipped: could not read open positions")
        return []
    open_tickets = {p["ticket"] for p in positions}
    events = []

    for trade in trades:
        ticket = trade.get("ticket")
        if not ticket or ticket in open_tickets:
            continue

        deals = get_position_deals(ticket)
        if not deals:
            # Position gone but no history yet: leave it for the next scan.
            continue

        # DEAL_ENTRY_OUT (1) / DEAL_ENTRY_OUT_BY (3). Without an exit deal the
        # position is not proven closed (history may lag), so retry next scan.
        exits = [d for d in deals if int(d.get("entry") or 0) in (1, 3)]
        if not exits:
            continue
        profit = sum(
            float(d.get("profit") or 0.0)
            + float(d.get("swap") or 0.0)
            + float(d.get("commission") or 0.0)
            for d in deals
        )
        exit_price = float(exits[-1]["price"])

        status = "win" if profit > 0 else "loss" if profit < 0 else "breakeven"
        if not close_live_trade(trade["id"], exit_price, profit, status):
            continue

        log.warning(
            "LIVE TRADE CLOSED: #%s %s %s @ %s → %s (%+.2f)",
            trade["id"], trade["symbol"], str(trade["direction"]).upper(),
            trade.get("entry"), exit_price, profit,
        )
        events.append({
            "trade": {**trade, "exit_price": exit_price, "profit": profit,
                      "status": status},
            "profit": profit,
        })

    return events
