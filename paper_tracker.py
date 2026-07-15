"""Simple $100-per-alert paper-trading ledger."""

import json
import os
from datetime import datetime, timezone

PAPER_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "paper_trades.json"
)
DEFAULT_STAKE = 100.0


def signal_id(signal):
    return f"{signal.get('slug', '')}::{signal.get('outcome', '')}"


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def signal_price(signal):
    """Return a position-value-weighted current price for a consensus signal."""
    priced_holders = []
    for holder in signal.get("holders", []):
        price = _number(holder.get("cur_price"))
        weight = _number(holder.get("value"))
        if price is not None and price > 0:
            priced_holders.append((price, weight if weight and weight > 0 else 1.0))

    if not priced_holders:
        return None

    total_weight = sum(weight for _, weight in priced_holders)
    return sum(price * weight for price, weight in priced_holders) / total_weight


def load_paper_trades():
    if not os.path.exists(PAPER_FILE):
        return {"stake_per_trade": DEFAULT_STAKE, "trades": []}
    try:
        with open(PAPER_FILE, "r") as file:
            data = json.load(file)
        if not isinstance(data, dict) or not isinstance(data.get("trades"), list):
            raise ValueError("invalid paper ledger")
        return data
    except (OSError, ValueError, json.JSONDecodeError):
        return {"stake_per_trade": DEFAULT_STAKE, "trades": []}


def save_paper_trades(ledger):
    temporary = PAPER_FILE + ".tmp"
    with open(temporary, "w") as file:
        json.dump(ledger, file, indent=2)
    os.replace(temporary, PAPER_FILE)


def update_paper_trades(signals, new_signals, lost_signals=None, stake=DEFAULT_STAKE):
    """Add new alerts and mark existing paper trades to current signal prices."""
    ledger = load_paper_trades()
    ledger["stake_per_trade"] = float(stake)
    trades = ledger["trades"]
    by_id = {trade.get("signal_id"): trade for trade in trades}
    now = datetime.now(timezone.utc).isoformat()
    lost_ids = {signal_id(signal) for signal in (lost_signals or [])}

    # Mark existing trades to the latest price while their signal is visible.
    for signal in signals:
        key = signal_id(signal)
        trade = by_id.get(key)
        price = signal_price(signal)
        if not trade or price is None:
            continue
        trade["current_price"] = round(price, 6)
        trade["current_value"] = round(trade["shares"] * price, 2)
        trade["paper_pnl"] = round(trade["current_value"] - trade["stake"], 2)
        trade["return_percent"] = round(100 * trade["paper_pnl"] / trade["stake"], 2)
        trade["last_seen"] = now
        trade["status"] = "OPEN"

    for key in lost_ids:
        trade = by_id.get(key)
        if trade:
            trade["status"] = "CONSENSUS_LOST"
            trade["consensus_lost_at"] = now

    added = []
    for signal in new_signals:
        key = signal_id(signal)
        if key in by_id:
            continue
        price = signal_price(signal)
        if price is None or price <= 0:
            continue
        shares = float(stake) / price
        trade = {
            "signal_id": key,
            "market": signal.get("market"),
            "slug": signal.get("slug"),
            "side": signal.get("outcome"),
            "alerted_at": now,
            "last_seen": now,
            "entry_price": round(price, 6),
            "current_price": round(price, 6),
            "stake": float(stake),
            "shares": round(shares, 6),
            "current_value": float(stake),
            "paper_pnl": 0.0,
            "return_percent": 0.0,
            "status": "OPEN",
            "num_traders": signal.get("num_traders"),
            "combined_value": signal.get("total_value"),
            "traders": [holder.get("trader") for holder in signal.get("holders", [])],
            "url": "https://polymarket.com/event/" + str(signal.get("slug", "")),
        }
        trades.append(trade)
        by_id[key] = trade
        added.append(trade)

    ledger["updated_at"] = now
    save_paper_trades(ledger)
    return ledger, added


def paper_summary(ledger):
    trades = ledger.get("trades", [])
    total_staked = sum(_number(trade.get("stake")) or 0 for trade in trades)
    total_value = sum(_number(trade.get("current_value")) or 0 for trade in trades)
    pnl = total_value - total_staked
    return {
        "trades": len(trades),
        "total_staked": round(total_staked, 2),
        "current_value": round(total_value, 2),
        "paper_pnl": round(pnl, 2),
        "return_percent": round(100 * pnl / total_staked, 2) if total_staked else 0.0,
    }
