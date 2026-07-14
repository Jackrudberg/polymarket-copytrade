"""One cloud-safe scan used by the scheduled GitHub Actions workflow."""

import os

from copytrade_scanner_v2 import (
    load_previous_state,
    print_signals,
    save_state,
    scan,
    send_webhook,
    signal_id,
)


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

    # The first cloud run establishes a baseline so it does not send a large
    # batch of alerts for positions that were already present before launch.
    if previous_ids:
        new_signals = [s for s in signals if signal_id(s) not in previous_ids]
        if new_signals:
            print_signals(new_signals, "NEW CLOUD CONSENSUS TRADES")
            send_webhook(webhook, new_signals)
        else:
            print("No new consensus trades since the previous cloud scan.")
    else:
        print(f"Cloud baseline created with {len(signals)} current signals.")

    save_state(signals)


if __name__ == "__main__":
    main()
