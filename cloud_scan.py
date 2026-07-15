"""One cloud-safe scan with a $100-per-alert paper-trading ledger."""

import os

from copytrade_scanner_v2 import (
    load_previous_state,
    print_signals,
    save_state,
    scan,
    send_exit_webhook,
    send_webhook,
    signal_id,
)
from paper_tracker import paper_summary, update_paper_trades


def main():
    webhook = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook:
        raise RuntimeError("DISCORD_WEBHOOK_URL secret is missing")

    previous = load_previous_state()
    previous_ids = set(previous.get("seen_ids", []))
    previous_signals = previous.get("signals", [])
    previous_by_id = {signal_id(signal): signal for signal in previous_signals}
    missing_counts = dict(previous.get("missing_counts", {}))

    signals = scan(
        top_n=100,
        min_volume=20_000,
        min_roi=0.15,
        min_traders_for_signal=3,
        min_position_value=100,
        verbose=True,
    )

    current_ids = {signal_id(signal) for signal in signals}
    for key in current_ids:
        missing_counts.pop(key, None)
    for key in previous_by_id:
        if key not in current_ids:
            missing_counts[key] = missing_counts.get(key, 0) + 1

    # Require two consecutive misses to reduce false exit alerts from a
    # temporary API failure or a single noisy scan.
    lost_signals = [
        signal
        for key, signal in previous_by_id.items()
        if missing_counts.get(key) == 2
    ]

    new_signals = []
    if previous_ids:
        new_signals = [s for s in signals if signal_id(s) not in previous_ids]
        if new_signals:
            print_signals(new_signals, "NEW CLOUD CONSENSUS TRADES")
            send_webhook(webhook, new_signals)
        else:
            print("No new consensus trades since the previous cloud scan.")
    else:
        print(f"Cloud baseline created with {len(signals)} current signals.")

    if lost_signals:
        print_signals(lost_signals, "CONSENSUS LOST FOR TWO CONSECUTIVE SCANS")
        send_exit_webhook(webhook, lost_signals)

    ledger, added = update_paper_trades(
        signals, new_signals, lost_signals=lost_signals, stake=100.0
    )
    summary = paper_summary(ledger)
    print(
        "Paper tracker: "
        f"{summary['trades']} trades, ${summary['total_staked']:.2f} staked, "
        f"${summary['paper_pnl']:+.2f} P&L ({summary['return_percent']:+.2f}%)."
    )
    if added:
        print(f"Added {len(added)} new $100 paper trade(s).")

    retained_missing = [
        signal
        for key, signal in previous_by_id.items()
        if key not in current_ids and missing_counts.get(key, 0) <= 2
    ]
    save_state(
        signals,
        missing_counts=missing_counts,
        tracked_signals=signals + retained_missing,
    )


if __name__ == "__main__":
    main()
