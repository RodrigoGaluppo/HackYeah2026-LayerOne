#!/usr/bin/env python3
"""Live CUSUM monitor for the GPIO4-triggered LayerOne ESP32 experiment."""

import argparse
import csv
import math
import statistics
import sys
import time
from collections import deque
from datetime import datetime

from acs_scope_cusum import acquire_window, robust_center_sigma, verify_scope


RED = "\033[91m"
GREEN = "\033[92m"
CYAN = "\033[96m"
YELLOW = "\033[93m"
BOLD = "\033[1m"
RESET = "\033[0m"


def color(text, code):
    return f"{code}{text}{RESET}" if sys.stdout.isatty() else text


def main():
    parser = argparse.ArgumentParser(
        description="Learn the quiet ACS baseline, detect a button-triggered "
                    "ESP32 workload, confirm recovery, and rearm."
    )
    parser.add_argument("--driver", default="hantek-6xxx")
    parser.add_argument("--channel", default="CH1")
    parser.add_argument("--samplerate", type=int, default=100_000)
    parser.add_argument("--capture-ms", type=float, default=20.0)
    parser.add_argument("--interval-ms", type=float, default=500.0)
    parser.add_argument("--baseline-seconds", type=float, default=8.0)
    parser.add_argument("--k-sigma", type=float, default=0.5)
    parser.add_argument("--h-sigma", type=float, default=5.0)
    parser.add_argument("--sigma-floor-mv", type=float, default=0.10)
    parser.add_argument("--vote-window", type=int, default=5)
    parser.add_argument("--votes-required", type=int, default=3)
    parser.add_argument("--recovery-windows", type=int, default=6)
    parser.add_argument("--log", default="button_cusum_log.csv")
    args = parser.parse_args()

    if args.baseline_seconds <= 1 or args.interval_ms <= 0:
        parser.error("baseline must exceed 1 second and interval must be positive")
    if not 1 <= args.votes_required <= args.vote_window:
        parser.error("votes-required must be between 1 and vote-window")

    print(color("\nLayerOne / Keypad-1 CUSUM Monitor", BOLD))
    print("=" * 72)
    print("Keep the button RELEASED while the quiet baseline is learned.")
    print("Close OpenHantek before running: this process owns the scope USB device.\n")

    if not verify_scope(args.driver):
        return 2

    interval_s = args.interval_ms / 1000.0
    baseline_count = max(4, math.ceil(args.baseline_seconds / interval_s))
    baseline = []

    print(color(f"CALIBRATING: {baseline_count} quiet windows", CYAN))
    for index in range(baseline_count):
        started = time.monotonic()
        mean_mv, _, low_mv, high_mv, _ = acquire_window(
            args.driver, args.channel, args.samplerate,
            args.capture_ms, 0.01
        )
        baseline.append(mean_mv)
        print(
            f"  {index + 1:02d}/{baseline_count:02d}  "
            f"mean={mean_mv:9.3f} mV  range={low_mv:8.1f}..{high_mv:8.1f}"
        )
        time.sleep(max(0.0, interval_s - (time.monotonic() - started)))

    mu, measured_sigma, _ = robust_center_sigma(baseline)
    sigma = max(measured_sigma, args.sigma_floor_mv)
    allowance = args.k_sigma * sigma
    threshold = args.h_sigma * sigma
    recovery_band = max(3.0 * sigma, 0.50)

    print()
    print(color(
        f"ARMED — baseline={mu:.3f} mV  sigma={sigma:.3f} mV  "
        f"CUSUM threshold={threshold:.3f} mV",
        GREEN,
    ))
    trigger_delta = max(3.0 * sigma, 1.0)
    print(
        f"Press keypad 1. Alarm requires {args.votes_required} of the latest "
        f"{args.vote_window} windows beyond ±{trigger_delta:.3f} mV; Ctrl-C exits.\n"
    )
    print("TIME          READING      DELTA      CUSUM+     CUSUM-   STATE")
    print("─" * 72)

    positive = 0.0
    negative = 0.0
    state = "ARMED"
    recovery_run = 0
    deviation_votes = deque(maxlen=args.vote_window)

    with open(args.log, "w", newline="", encoding="utf-8") as log_file:
        writer = csv.writer(log_file)
        writer.writerow([
            "timestamp", "mean_mv", "delta_mv", "cusum_positive_mv",
            "cusum_negative_mv", "state",
        ])

        try:
            while True:
                started = time.monotonic()
                mean_mv, _, _, _, _ = acquire_window(
                    args.driver, args.channel, args.samplerate,
                    args.capture_ms, 0.01
                )
                delta = mean_mv - mu

                positive = max(0.0, positive + delta - allowance)
                negative = max(0.0, negative - delta - allowance)

                if state == "ARMED":
                    vote = 0
                    if delta >= trigger_delta:
                        vote = 1
                    elif delta <= -trigger_delta:
                        vote = -1
                    deviation_votes.append(vote)

                    high_votes = deviation_votes.count(1)
                    low_votes = deviation_votes.count(-1)
                    direction = "HIGH" if high_votes >= low_votes else "LOW"
                    enough_votes = max(high_votes, low_votes) >= args.votes_required

                    if enough_votes and max(positive, negative) >= threshold:
                        state = "ANOMALY"
                        recovery_run = 0
                        print(color(
                            f"\n>>> CUSUM {direction} ALERT — keypad-1 workload detected <<<",
                            RED,
                        ))
                    elif len(deviation_votes) == args.vote_window and not enough_votes:
                        # A normal heartbeat contributes only two high votes.
                        # Once the full window proves it is not sustained,
                        # discard its large CUSUM contribution.
                        positive = 0.0
                        negative = 0.0

                if state == "ANOMALY":
                    if abs(delta) <= recovery_band:
                        recovery_run += 1
                    else:
                        recovery_run = 0

                    if recovery_run >= args.recovery_windows:
                        print(color(
                            f"\n<<< RECOVERED — {recovery_run} consecutive baseline windows; rearmed >>>",
                            GREEN,
                        ))
                        state = "ARMED"
                        positive = 0.0
                        negative = 0.0
                        recovery_run = 0
                        deviation_votes.clear()

                timestamp = datetime.now().astimezone().isoformat(timespec="milliseconds")
                state_text = color(state, RED if state == "ANOMALY" else GREEN)
                print(
                    f"{timestamp[11:23]}  {mean_mv:9.3f}  {delta:+9.3f}  "
                    f"{positive:9.3f}  {negative:9.3f}   {state_text}"
                )
                writer.writerow([
                    timestamp, f"{mean_mv:.6f}", f"{delta:.6f}",
                    f"{positive:.6f}", f"{negative:.6f}", state,
                ])
                log_file.flush()

                time.sleep(max(0.0, interval_s - (time.monotonic() - started)))
        except KeyboardInterrupt:
            print(color(f"\nStopped. CSV saved to {args.log}", YELLOW))
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
