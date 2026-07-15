"""One cloud-safe scan with a $100-per-alert paper-trading ledger."""

import os

from copytrade_scanner_v2 import (
    load_previous_state,
    print_signals,
    save_state,
    scan,
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

    signals = scan(
        top_n=100,
        min_volume=20_000,
        min_roi=0.15,
        min_traders_for_signal=3,
        min_position_value=100,
        verbose=True,
    )

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

    ledger, added = update_paper_trades(signals, new_signals, stake=100.0)
    summary = paper_summary(ledger)
    print(
        "Paper tracker: "
        f"{summary['trades']} trades, ${summary['total_staked']:.2f} staked, "
        f"${summary['paper_pnl']:+.2f} P&L ({summary['return_percent']:+.2f}%)."
    )
    if added:
        print(f"Added {len(added)} new $100 paper trade(s).")

    save_state(signals)


if __name__ == "__main__":
    main()
