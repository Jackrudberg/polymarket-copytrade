from datetime import datetime, timezone
import json
import os
import re

import pandas as pd
import requests
import streamlit as st

from copytrade_scanner_v2 import scan


GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
HISTORY_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "dashboard_history.json"
)
PAPER_TRADES_URL = (
    "https://raw.githubusercontent.com/Jackrudberg/"
    "polymarket-copytrade/main/paper_trades.json"
)


st.set_page_config(
    page_title="Polymarket Copy-Trade Dashboard",
    page_icon="📊",
    layout="wide",
)

st.title("Polymarket Copy-Trade Dashboard")
st.caption("Consensus positions held by qualifying leaderboard traders")


with st.sidebar:
    st.header("Scanner settings")

    top_n = st.number_input(
        "Top traders to examine", min_value=10, max_value=100, value=50, step=10
    )
    min_volume = st.number_input(
        "Minimum trader volume ($)", min_value=0, value=20000, step=5000
    )
    min_roi_percent = st.number_input(
        "Minimum trader ROI (%)", min_value=0, max_value=500, value=15, step=5
    )
    min_traders = st.number_input(
        "Minimum agreeing traders", min_value=2, max_value=20, value=3, step=1
    )
    min_position_value = st.number_input(
        "Minimum position value ($)", min_value=0, value=100, step=50
    )

    st.divider()
    st.caption(
        "Signal Score is a research filter, not a recommendation or guarantee."
    )


def number(value):
    """Convert an API value to float without crashing on blanks."""
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def json_list(value):
    """Gamma sometimes returns arrays and sometimes JSON-encoded arrays."""
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            result = json.loads(value)
            return result if isinstance(result, list) else []
        except json.JSONDecodeError:
            return []
    return []


def normalize(text):
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


def average_price(holders, field):
    prices = [number(holder.get(field)) for holder in holders]
    prices = [price for price in prices if price is not None]
    return sum(prices) / len(prices) if prices else None


def largest_trader_percent(holders, total_value):
    total = number(total_value) or 0
    values = [number(holder.get("value")) or 0 for holder in holders]
    if not values or total <= 0:
        return None
    return 100 * max(values) / total


def load_history():
    if not os.path.exists(HISTORY_FILE):
        return {}
    try:
        with open(HISTORY_FILE, "r") as file:
            data = json.load(file)
            return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_history(history):
    temp_file = HISTORY_FILE + ".tmp"
    with open(temp_file, "w") as file:
        json.dump(history, file, indent=2)
    os.replace(temp_file, HISTORY_FILE)


def fetch_event(slug):
    response = requests.get(
        f"{GAMMA_API}/events", params={"slug": slug}, timeout=15
    )
    response.raise_for_status()
    events = response.json()
    return events[0] if isinstance(events, list) and events else None


def fetch_market(slug):
    """Look up an individual Gamma market by its market slug."""
    if not slug:
        return None
    response = requests.get(
        f"{GAMMA_API}/markets", params={"slug": slug}, timeout=15
    )
    response.raise_for_status()
    markets = response.json()
    return markets[0] if isinstance(markets, list) and markets else None


def resolve_market(signal):
    """Resolve either kind of slug returned by the positions API.

    Position records normally provide a market slug, while the original
    dashboard treated every slug as an event slug. Try the direct market
    endpoint first, then retain event lookup as a fallback for older records.
    """
    slug = signal.get("slug", "")

    market = fetch_market(slug)
    if market:
        return None, market

    event = fetch_event(slug)
    return event, matching_market(event, signal)


def matching_market(event, signal):
    if not event:
        return None

    markets = event.get("markets") or []
    if len(markets) == 1:
        return markets[0]

    wanted = normalize(signal.get("market"))
    for market in markets:
        candidates = [
            market.get("question"),
            market.get("title"),
            market.get("groupItemTitle"),
            market.get("slug"),
        ]
        normalized = [normalize(candidate) for candidate in candidates if candidate]
        if wanted in normalized:
            return market

    for market in markets:
        question = normalize(market.get("question"))
        if wanted and question and (wanted in question or question in wanted):
            return market

    return None


def outcome_token_id(market, outcome):
    if not market:
        return None
    outcomes = json_list(market.get("outcomes"))
    token_ids = json_list(market.get("clobTokenIds"))
    wanted = normalize(outcome)
    for index, label in enumerate(outcomes):
        if normalize(label) == wanted and index < len(token_ids):
            return str(token_ids[index])
    return None


def fetch_spread(token_id):
    if not token_id:
        return None
    try:
        response = requests.get(
            f"{CLOB_API}/spread", params={"token_id": token_id}, timeout=10
        )
        response.raise_for_status()
        payload = response.json()
        return number(payload.get("spread"))
    except (requests.RequestException, ValueError):
        return None


def parse_end_date(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except ValueError:
        return None


def calculate_score(row):
    """Transparent 0-10 research score based only on displayed fields."""
    score = 0

    traders = row.get("Traders") or 0
    score += 3 if traders >= 5 else 2 if traders == 4 else 1 if traders == 3 else 0

    value = row.get("Combined Value") or 0
    score += 2 if value >= 100000 else 1 if value >= 25000 else 0

    entry = row.get("Average Entry")
    current = row.get("Current Price")
    if entry is not None and current is not None:
        chase = current - entry
        score += 2 if chase <= 0.05 else 1 if chase <= 0.10 else 0

    concentration = row.get("Largest Trader %")
    if concentration is not None and concentration <= 50:
        score += 1

    liquidity = row.get("Liquidity")
    spread = row.get("Spread")
    if liquidity is not None and liquidity >= 10000 and spread is not None and spread <= 0.05:
        score += 1

    days_left = row.get("Days Left")
    if days_left is not None and 2 <= days_left <= 30:
        score += 1

    return min(score, 10)


def enrich_signals(signals):
    history = load_history()
    now = datetime.now(timezone.utc)
    rows = []

    for signal in signals:
        holders = signal.get("holders", [])
        current_price = average_price(holders, "cur_price")
        average_entry = average_price(holders, "avg_price")
        key = f"{signal.get('slug', '')}::{signal.get('outcome', '')}"

        record = history.get(key, {})
        if not record:
            record = {
                "first_seen": now.isoformat(),
                "first_price": current_price,
            }
        record["last_seen"] = now.isoformat()
        record["last_price"] = current_price
        history[key] = record

        event = None
        market = None
        try:
            event, market = resolve_market(signal)
        except (requests.RequestException, ValueError, TypeError):
            pass

        token_id = outcome_token_id(market, signal.get("outcome"))
        spread = fetch_spread(token_id)

        end_value = None
        if market:
            end_value = market.get("endDate")
        if not end_value and event:
            end_value = event.get("endDate")
        end_date = parse_end_date(end_value)
        days_left = None
        if end_date:
            days_left = max(0, (end_date - now).total_seconds() / 86400)

        liquidity = None
        if market:
            liquidity = number(market.get("liquidityNum"))
            if liquidity is None:
                liquidity = number(market.get("liquidity"))
        if liquidity is None and event:
            liquidity = number(event.get("liquidity"))

        first_price = number(record.get("first_price"))
        change_since_seen = None
        if current_price is not None and first_price is not None:
            change_since_seen = current_price - first_price

        row = {
            "Market": signal.get("market"),
            "Side": signal.get("outcome"),
            "Traders": signal.get("num_traders"),
            "Combined Value": signal.get("total_value"),
            "Largest Trader %": largest_trader_percent(
                holders, signal.get("total_value")
            ),
            "Average Entry": average_entry,
            "Current Price": current_price,
            "First Seen": record.get("first_seen"),
            "Change Since Seen": change_since_seen,
            "Ends": end_date,
            "Days Left": days_left,
            "Liquidity": liquidity,
            "Spread": spread,
            "Polymarket": "https://polymarket.com/event/" + signal.get("slug", ""),
        }
        row["Signal Score"] = calculate_score(row)
        rows.append(row)

    save_history(history)
    return rows


scan_now = st.button("Scan now", type="primary", width="stretch")

if scan_now:
    try:
        with st.spinner("Scanning traders and loading market details..."):
            signals = scan(
                int(top_n),
                float(min_volume),
                float(min_roi_percent) / 100,
                int(min_traders),
                float(min_position_value),
                verbose=False,
            )
            rows = enrich_signals(signals)

        st.session_state["signals"] = signals
        st.session_state["rows"] = rows
        st.session_state["last_scan"] = datetime.now()

    except requests.RequestException as error:
        st.error(f"Polymarket data request failed: {error}")
    except Exception as error:
        st.error(f"Scanner error: {error}")


signals = st.session_state.get("signals")
rows = st.session_state.get("rows")

if signals is None:
    st.info("Choose your settings, then click Scan now.")
elif not signals:
    st.warning("No consensus trades matched the current settings.")
else:
    table = pd.DataFrame(rows)
    last_scan = st.session_state.get("last_scan")
    if last_scan:
        st.success(
            f"Found {len(rows)} consensus trades. "
            f"Last scanned at {last_scan.strftime('%I:%M:%S %p')}."
        )

    st.dataframe(
        table,
        width="stretch",
        hide_index=True,
        column_config={
            "Combined Value": st.column_config.NumberColumn(format="$%.2f"),
            "Largest Trader %": st.column_config.NumberColumn(format="%.1f%%"),
            "Average Entry": st.column_config.NumberColumn(format="%.3f"),
            "Current Price": st.column_config.NumberColumn(format="%.3f"),
            "Change Since Seen": st.column_config.NumberColumn(format="%+.3f"),
            "First Seen": st.column_config.DatetimeColumn(format="MM/DD/YY h:mm a"),
            "Ends": st.column_config.DatetimeColumn(format="MM/DD/YY h:mm a"),
            "Days Left": st.column_config.NumberColumn(format="%.1f"),
            "Liquidity": st.column_config.NumberColumn(format="$%.0f"),
            "Spread": st.column_config.NumberColumn(format="%.3f"),
            "Signal Score": st.column_config.ProgressColumn(
                min_value=0, max_value=10, format="%d/10"
            ),
            "Polymarket": st.column_config.LinkColumn(display_text="Open market"),
        },
    )

    st.caption(
        "Change Since Seen starts at zero on the first upgraded scan. "
        "Blank market fields mean Polymarket did not return a matching value."
    )

    st.subheader("Trader details")
    for signal in signals:
        title = (
            f"{signal['market']} → {signal['outcome']} "
            f"({signal['num_traders']} traders)"
        )
        with st.expander(title):
            holder_table = pd.DataFrame(signal["holders"])
            holder_table = holder_table.rename(
                columns={
                    "trader": "Trader",
                    "roi": "Lifetime ROI",
                    "value": "Position Value",
                    "avg_price": "Average Entry",
                    "cur_price": "Current Price",
                }
            )
            st.dataframe(holder_table, width="stretch", hide_index=True)


st.divider()
st.header("$100 Paper Trading")
st.caption("Tracks every new cloud consensus alert as an equal $100 virtual position.")

try:
    paper_response = requests.get(PAPER_TRADES_URL, timeout=15)
    paper_response.raise_for_status()
    paper_ledger = paper_response.json()
    paper_trades = paper_ledger.get("trades", [])

    total_staked = sum(float(trade.get("stake", 0) or 0) for trade in paper_trades)
    current_value = sum(float(trade.get("current_value", 0) or 0) for trade in paper_trades)
    paper_pnl = current_value - total_staked
    paper_return = 100 * paper_pnl / total_staked if total_staked else 0.0
    winners = sum(1 for trade in paper_trades if float(trade.get("paper_pnl", 0) or 0) > 0)
    win_rate = 100 * winners / len(paper_trades) if paper_trades else 0.0

    metric_columns = st.columns(5)
    metric_columns[0].metric("Paper Trades", len(paper_trades))
    metric_columns[1].metric("Virtual Invested", f"${total_staked:,.2f}")
    metric_columns[2].metric("Current Value", f"${current_value:,.2f}", delta=f"${paper_pnl:+,.2f}")
    metric_columns[3].metric("Total Return", f"{paper_return:+.2f}%")
    metric_columns[4].metric("Current Win Rate", f"{win_rate:.1f}%")

    if not paper_trades:
        st.info("The tracker is active. The first row will appear when the cloud scanner finds a genuinely new consensus alert.")
    else:
        paper_table = pd.DataFrame(paper_trades)
        display_columns = ["market", "side", "alerted_at", "entry_price", "current_price", "stake", "current_value", "paper_pnl", "return_percent", "num_traders", "url"]
        display_columns = [column for column in display_columns if column in paper_table.columns]
        paper_table = paper_table[display_columns].rename(columns={
            "market": "Market", "side": "Side", "alerted_at": "Alerted At",
            "entry_price": "Entry Price", "current_price": "Current Price",
            "stake": "Virtual Stake", "current_value": "Current Value",
            "paper_pnl": "Paper P&L", "return_percent": "Return %",
            "num_traders": "Traders", "url": "Polymarket",
        })
        st.dataframe(
            paper_table, width="stretch", hide_index=True,
            column_config={
                "Alerted At": st.column_config.DatetimeColumn(format="MM/DD/YY h:mm a"),
                "Entry Price": st.column_config.NumberColumn(format="%.3f"),
                "Current Price": st.column_config.NumberColumn(format="%.3f"),
                "Virtual Stake": st.column_config.NumberColumn(format="$%.2f"),
                "Current Value": st.column_config.NumberColumn(format="$%.2f"),
                "Paper P&L": st.column_config.NumberColumn(format="$%+.2f"),
                "Return %": st.column_config.NumberColumn(format="%+.2f%%"),
                "Polymarket": st.column_config.LinkColumn(display_text="Open market"),
            },
        )
except (requests.RequestException, ValueError, TypeError) as error:
    st.warning(f"Paper-trading results could not be loaded from GitHub: {error}")

