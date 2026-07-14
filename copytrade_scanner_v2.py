"""
Polymarket Copy-Trade Scanner v2
----------------------------------
- Pulls top traders from the public leaderboard
- Filters them by a quality bar (min volume, min ROI = pnl/volume)
- Fetches each qualifying trader's open positions
- Aggregates positions into "consensus trades" (N+ quality traders on the
  same side of the same market)
- Can run once, or in --watch mode: polls on an interval, diffs against the
  previous scan, and alerts (console + optional webhook) only on NEW
  consensus trades that weren't there last time

No API key or wallet needed - all endpoints are public read-only data.

Usage:
    pip install requests --break-system-packages

    # one-off scan
    python copytrade_scanner_v2.py

    # continuous watch mode, checking every 10 minutes
    python copytrade_scanner_v2.py --watch --interval 600

    # send alerts to a Slack/Discord webhook too
    python copytrade_scanner_v2.py --watch --webhook https://hooks.slack.com/services/XXX
"""

import argparse
import json
import os
import time
from collections import defaultdict
from datetime import datetime, timezone

import requests

DATA_API = "https://data-api.polymarket.com"
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scanner_state.json")

# ---- Default config (overridable via CLI flags) -------------------------
TOP_N_TRADERS = 50           # how many leaderboard traders to pull before filtering
MIN_VOLUME = 20_000          # ignore traders with less than this much lifetime volume ($)
MIN_ROI = 0.15               # ignore traders with pnl/volume below this (15%)
MIN_TRADERS_FOR_SIGNAL = 3   # markets need this many *qualifying* traders agreeing
MIN_POSITION_VALUE = 100     # ignore dust positions under $100 current value
REQUEST_DELAY = 0.3          # be polite to the API between calls
# ---------------------------------------------------------------------------


def get_leaderboard(n):
    resp = requests.get(f"{DATA_API}/v1/leaderboard", params={"limit": n})
    resp.raise_for_status()
    return resp.json()


def get_positions(wallet):
    resp = requests.get(
        f"{DATA_API}/positions",
        params={
            "user": wallet,
            "limit": 100,
            "sortBy": "CURRENT",
            "sortDirection": "DESC",
        },
    )
    resp.raise_for_status()
    return resp.json()


def qualify_traders(traders, min_volume, min_roi):
    """Filter leaderboard entries down to traders that clear a quality bar.

    Note: Polymarket's public leaderboard doesn't expose a true win-rate
    (that requires reconstructing every resolved trade). ROI here is a
    practical proxy: lifetime pnl / lifetime volume. Combined with a
    minimum volume floor, this filters out traders who got lucky on a
    single small bet (high ROI, tiny volume) and keeps traders who have
    sustained, sizeable profits.
    """
    qualified = []
    for t in traders:
        vol = t.get("vol", 0) or 0
        pnl = t.get("pnl", 0) or 0
        if vol < min_volume:
            continue
        roi = pnl / vol if vol else 0
        if roi < min_roi:
            continue
        t["_roi"] = round(roi, 4)
        qualified.append(t)
    return qualified


def scan(top_n, min_volume, min_roi, min_traders_for_signal, min_position_value, verbose=True):
    if verbose:
        print(f"Fetching top {top_n} traders from leaderboard...")
    traders = get_leaderboard(top_n)
    if not traders:
        if verbose:
            print("No traders returned - check the leaderboard endpoint/params.")
        return []

    qualified = qualify_traders(traders, min_volume, min_roi)
    if verbose:
        print(f"{len(qualified)}/{len(traders)} traders passed quality filters "
              f"(min volume ${min_volume:,}, min ROI {min_roi:.0%})")

    aggregated = defaultdict(lambda: defaultdict(list))

    for t in qualified:
        wallet = t.get("proxyWallet")
        name = t.get("userName") or (wallet[:8] if wallet else "unknown")
        if not wallet:
            continue
        try:
            positions = get_positions(wallet)
        except requests.RequestException as e:
            if verbose:
                print(f"  ! failed to fetch positions for {name}: {e}")
            continue

        for pos in positions:
            value = pos.get("currentValue", 0)
            if value < min_position_value:
                continue
            key = (pos.get("title", "Unknown market"), pos.get("slug", ""))
            aggregated[key][pos.get("outcome", "?")].append(
                {
                    "trader": name,
                    "roi": t["_roi"],
                    "value": round(value, 2),
                    "avg_price": pos.get("avgPrice"),
                    "cur_price": pos.get("curPrice"),
                }
            )
        time.sleep(REQUEST_DELAY)

    signals = []
    for (title, slug), outcomes in aggregated.items():
        for outcome, holders in outcomes.items():
            if len(holders) >= min_traders_for_signal:
                signals.append(
                    {
                        "market": title,
                        "slug": slug,
                        "outcome": outcome,
                        "num_traders": len(holders),
                        "total_value": round(sum(h["value"] for h in holders), 2),
                        "holders": sorted(holders, key=lambda h: h["value"], reverse=True),
                    }
                )

    signals.sort(key=lambda s: (s["num_traders"], s["total_value"]), reverse=True)
    return signals


def print_signals(signals, header="CONSENSUS TRADES"):
    print(f"\n{'='*80}\n{header}\n{'='*80}\n")
    if not signals:
        print("None found with current thresholds.\n")
        return
    for s in signals:
        print(f"{s['market']}  ->  {s['outcome']}")
        print(f"  {s['num_traders']} qualifying traders | combined position value ~${s['total_value']:,}")
        print(f"  https://polymarket.com/event/{s['slug']}")
        for h in s["holders"]:
            print(f"    - {h['trader']} (lifetime ROI {h['roi']:.0%}): "
                  f"${h['value']:,} (avg entry {h['avg_price']}, current {h['cur_price']})")
        print()


def signal_id(s):
    """A stable key for a signal, used to detect 'new' vs 'already seen'."""
    return f"{s['slug']}::{s['outcome']}"


def load_previous_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {"seen_ids": []}


def save_state(signals):
    with open(STATE_FILE, "w") as f:
        json.dump({"seen_ids": [signal_id(s) for s in signals],
                    "updated_at": datetime.now(timezone.utc).isoformat()}, f)


def send_webhook(webhook_url, new_signals):
    lines = [f"*New Polymarket consensus trade(s) found:*"]
    for s in new_signals:
        lines.append(
            f"• *{s['market']}* -> *{s['outcome']}* "
            f"({s['num_traders']} traders, ~${s['total_value']:,}) "
            f"https://polymarket.com/event/{s['slug']}"
        )
    payload = {"text": "\n".join(lines)}
    try:
        requests.post(webhook_url, json=payload, timeout=10)
    except requests.RequestException as e:
        print(f"  ! webhook delivery failed: {e}")


def run_once(args):
    signals = scan(args.top_n, args.min_volume, args.min_roi,
                    args.min_traders, args.min_position_value)
    print_signals(signals)
    save_state(signals)


def run_watch(args):
    print(f"Starting watch mode - checking every {args.interval}s. Ctrl+C to stop.\n")
    while True:
        try:
            timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            print(f"\n[{timestamp}] Scanning...")

            prev_state = load_previous_state()
            prev_ids = set(prev_state.get("seen_ids", []))

            signals = scan(args.top_n, args.min_volume, args.min_roi,
                            args.min_traders, args.min_position_value, verbose=False)

            new_signals = [s for s in signals if signal_id(s) not in prev_ids]

            if new_signals:
                print_signals(new_signals, header="NEW CONSENSUS TRADES SINCE LAST SCAN")
                if args.webhook:
                    send_webhook(args.webhook, new_signals)
            else:
                print("No new consensus trades since last scan.")

            save_state(signals)

        except requests.RequestException as e:
            print(f"  ! scan failed: {e}")

        time.sleep(args.interval)


def main():
    parser = argparse.ArgumentParser(description="Polymarket copy-trade scanner")
    parser.add_argument("--top-n", type=int, default=TOP_N_TRADERS,
                         help="How many leaderboard traders to pull before filtering")
    parser.add_argument("--min-volume", type=float, default=MIN_VOLUME,
                         help="Minimum lifetime trading volume ($) to qualify")
    parser.add_argument("--min-roi", type=float, default=MIN_ROI,
                         help="Minimum lifetime ROI (pnl/volume) to qualify, e.g. 0.15 = 15%%")
    parser.add_argument("--min-traders", type=int, default=MIN_TRADERS_FOR_SIGNAL,
                         help="Minimum number of qualifying traders agreeing to flag a signal")
    parser.add_argument("--min-position-value", type=float, default=MIN_POSITION_VALUE,
                         help="Ignore positions worth less than this ($)")
    parser.add_argument("--watch", action="store_true", help="Run continuously and alert on new signals")
    parser.add_argument("--interval", type=int, default=600, help="Seconds between scans in --watch mode")
    parser.add_argument("--webhook", type=str, default=None,
                         help="Optional Slack/Discord-compatible webhook URL for alerts")
    args = parser.parse_args()

    if args.watch:
        run_watch(args)
    else:
        run_once(args)


if __name__ == "__main__":
    main()
