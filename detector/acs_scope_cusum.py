#!/usr/bin/env python3
"""
LayerOne MVP - Hantek 6022BE / ACS sensor CUSUM detector

Reads CH1 through sigrok-cli, learns a normal baseline, then detects
persistent voltage/current-sensor shifts using a two-sided CUSUM.

Close OpenHantek before running this script: both programs cannot own the
same USB oscilloscope at the same time.
"""

import argparse
import csv
import math
import os
import shutil
import statistics
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


RED = "\033[91m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"


def paint(s, c):
    return f"{c}{s}{RESET}" if sys.stdout.isatty() else s


def run_cmd(cmd, timeout=None):
    env = os.environ.copy()
    env["LC_ALL"] = "C"
    env["LANG"] = "C"
    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
        timeout=timeout,
        env=env,
    )


def parse_sigrok_voltage_mv(line, expected_channel="CH1"):
    """
    Robust parser for lines such as:
        CH1: 0.07 V
        CH1: -1.25 V
        CH1: 17.3 mV

    Avoids regex entirely so formatting variations are less fragile.
    """
    line = line.strip()
    if not line or ":" not in line:
        return None

    lhs, rhs = line.split(":", 1)
    if lhs.strip().upper() != expected_channel.upper():
        return None

    parts = rhs.strip().split()
    if len(parts) < 2:
        return None

    try:
        value = float(parts[0])
    except ValueError:
        return None

    unit = parts[1]

    if unit == "V":
        return value * 1000.0
    if unit == "mV":
        return value
    if unit in ("uV", "µV"):
        return value / 1000.0
    if unit == "nV":
        return value / 1_000_000.0

    return None


def robust_center_sigma(values):
    med = statistics.median(values)
    mad = statistics.median(abs(x - med) for x in values)
    sigma = 1.4826 * mad

    if sigma <= 1e-12 and len(values) > 1:
        sigma = statistics.pstdev(values)

    return med, sigma, mad


def trimmed_mean(values, trim_fraction=0.01):
    """
    Mean after dropping extreme capture samples.

    Useful because some Hantek/sigrok captures can contain a few startup
    outliers. With 1% trimming, the sustained signal is preserved while
    isolated extreme samples have much less influence.
    """
    if not values:
        raise ValueError("empty sample list")

    if len(values) < 20 or trim_fraction <= 0:
        return statistics.fmean(values)

    ordered = sorted(values)
    cut = int(len(ordered) * trim_fraction)

    if cut == 0 or 2 * cut >= len(ordered):
        return statistics.fmean(ordered)

    return statistics.fmean(ordered[cut:-cut])


def format_samplerate(rate):
    if rate >= 1_000_000 and rate % 1_000_000 == 0:
        return f"{rate // 1_000_000}m"
    if rate >= 1000 and rate % 1000 == 0:
        return f"{rate // 1000}k"
    return str(rate)


def verify_scope(driver):
    if shutil.which("sigrok-cli") is None:
        print(paint("ERROR: sigrok-cli not found.", RED))
        print("Install it with:")
        print("  sudo apt install sigrok-cli sigrok-firmware-fx2lafw")
        return False

    result = run_cmd(
        ["sigrok-cli", "--driver", driver, "--show"],
        timeout=10,
    )

    combined = (result.stdout or "") + "\n" + (result.stderr or "")

    if result.returncode == 0 and "Hantek 6022" in combined:
        print(paint("Hantek 6022 detected by sigrok.", GREEN))
        return True

    print(paint("ERROR: sigrok could not open the Hantek.", RED))
    print("Close OpenHantek, unplug/replug the scope, and retry.")
    print(combined.strip())
    return False


def acquire_window(driver, channel, samplerate, capture_ms, trim_fraction):
    """
    Starts one short sigrok capture and returns:
      (trimmed_mean_mV, median_mV, min_mV, max_mV, sample_count)
    """
    samples = max(100, int(samplerate * capture_ms / 1000.0))

    cmd = [
        "sigrok-cli",
        "--driver", driver,
        "--channels", channel,
        "--config", f"samplerate={format_samplerate(samplerate)}",
        "--samples", str(samples),
    ]

    result = run_cmd(cmd, timeout=max(10, capture_ms / 1000.0 + 5))

    if result.returncode != 0:
        msg = (result.stderr or result.stdout or "").strip()
        raise RuntimeError("sigrok acquisition failed:\n" + msg)

    values = []

    for line in result.stdout.splitlines():
        v = parse_sigrok_voltage_mv(line, channel)
        if v is not None and math.isfinite(v):
            values.append(v)

    if not values:
        preview = "\n".join(result.stdout.splitlines()[:20])
        raise RuntimeError(
            "No samples parsed. First sigrok lines were:\n" + preview
        )

    return (
        trimmed_mean(values, trim_fraction),
        statistics.median(values),
        min(values),
        max(values),
        len(values),
    )


class Cusum:
    def __init__(self, mu, sigma, k_sigma=0.5, h_sigma=5.0,
                 sigma_floor_mv=0.10):
        self.mu = mu
        self.sigma = max(sigma, sigma_floor_mv)
        self.k = k_sigma * self.sigma
        self.h = h_sigma * self.sigma
        self.pos = 0.0
        self.neg = 0.0

    def update(self, x):
        self.pos = max(0.0, self.pos + (x - self.mu) - self.k)
        self.neg = max(0.0, self.neg + (self.mu - x) - self.k)

        if self.pos > self.h:
            return "HIGH"
        if self.neg > self.h:
            return "LOW"
        return ""

    def reset(self):
        self.pos = 0.0
        self.neg = 0.0


def main():
    ap = argparse.ArgumentParser(
        description="LayerOne MVP Hantek/ACS live CUSUM detector"
    )

    ap.add_argument("--driver", default="hantek-6xxx")
    ap.add_argument("--channel", default="CH1")
    ap.add_argument("--samplerate", type=int, default=100_000)
    ap.add_argument("--capture-ms", type=float, default=20.0)
    ap.add_argument("--interval-ms", type=float, default=500.0)
    ap.add_argument("--baseline-seconds", type=float, default=120.0)
    ap.add_argument("--trim", type=float, default=0.01,
                    help="fraction trimmed from each end of each capture")
    ap.add_argument("--k-sigma", type=float, default=0.5)
    ap.add_argument("--h-sigma", type=float, default=5.0)
    ap.add_argument("--sigma-floor-mv", type=float, default=0.10)
    ap.add_argument("--log", default="cusum_log.csv")
    ap.add_argument("--reset-after-alert", action="store_true")

    args = ap.parse_args()

    if args.capture_ms <= 0:
        ap.error("--capture-ms must be > 0")
    if args.interval_ms <= 0:
        ap.error("--interval-ms must be > 0")
    if args.baseline_seconds <= 1:
        ap.error("--baseline-seconds must be > 1")
    if not (0 <= args.trim < 0.49):
        ap.error("--trim must be between 0 and 0.49")

    print()
    print(paint("LayerOne MVP / ACS Sensor CUSUM", BOLD))
    print("=" * 68)
    print("Close OpenHantek before running this.")
    print("Keep the ESP32 NORMAL while the baseline is learned.")
    print()

    if not verify_scope(args.driver):
        return 2

    # ------------------- Learn baseline -------------------
    baseline = []
    interval_s = args.interval_ms / 1000.0
    start = time.monotonic()
    next_tick = start
    last_print = -999.0

    print()
    print(paint(
        f"Learning baseline for {args.baseline_seconds:.0f} seconds...",
        CYAN
    ))
    print("Do not trigger your workload yet.")
    print()

    try:
        while True:
            elapsed = time.monotonic() - start
            if elapsed >= args.baseline_seconds:
                break

            now = time.monotonic()
            if now < next_tick:
                time.sleep(next_tick - now)

            mean_mv, med_mv, min_mv, max_mv, n = acquire_window(
                args.driver,
                args.channel,
                args.samplerate,
                args.capture_ms,
                args.trim,
            )

            baseline.append(mean_mv)

            elapsed = time.monotonic() - start
            if elapsed - last_print >= 2.0 or len(baseline) == 1:
                print(
                    f"baseline {elapsed:6.1f}/{args.baseline_seconds:.0f}s | "
                    f"avg {mean_mv:8.3f} mV | "
                    f"med {med_mv:8.3f} | "
                    f"range {min_mv:8.1f}..{max_mv:8.1f} | "
                    f"n={n}"
                )
                last_print = elapsed

            next_tick += interval_s

    except KeyboardInterrupt:
        print("\nStopped during baseline.")
        return 130
    except Exception as exc:
        print(paint(f"\nAcquisition error: {exc}", RED))
        return 1

    if len(baseline) < 5:
        print(paint("Not enough baseline windows collected.", RED))
        return 1

    mu, sigma, mad = robust_center_sigma(baseline)

    detector = Cusum(
        mu,
        sigma,
        args.k_sigma,
        args.h_sigma,
        args.sigma_floor_mv,
    )

    print()
    print(paint("BASELINE LEARNED", GREEN))
    print("=" * 68)
    print(f"Baseline median : {mu:.3f} mV")
    print(f"MAD             : {mad:.3f} mV")
    print(f"Robust sigma    : {sigma:.3f} mV")
    print(f"Sigma used      : {detector.sigma:.3f} mV")
    print(f"CUSUM k         : {detector.k:.3f} mV/window")
    print(f"CUSUM h         : {detector.h:.3f} mV")
    print()
    print("Monitoring. Trigger the ESP32 workload whenever you want.")
    print("Ctrl+C stops and keeps the CSV log.")
    print()

    # ------------------- Monitor -------------------
    log_path = Path(args.log)
    new_log = not log_path.exists()
    start = time.monotonic()
    next_tick = start

    try:
        with log_path.open("a", newline="", buffering=1) as fh:
            writer = csv.writer(fh)

            if new_log:
                writer.writerow([
                    "timestamp",
                    "elapsed_s",
                    "mean_mv",
                    "median_mv",
                    "min_mv",
                    "max_mv",
                    "delta_mv",
                    "baseline_mv",
                    "sigma_mv",
                    "cusum_pos",
                    "cusum_neg",
                    "anomaly",
                    "samples",
                ])

            while True:
                now = time.monotonic()
                if now < next_tick:
                    time.sleep(next_tick - now)

                mean_mv, med_mv, min_mv, max_mv, n = acquire_window(
                    args.driver,
                    args.channel,
                    args.samplerate,
                    args.capture_ms,
                    args.trim,
                )

                delta = mean_mv - detector.mu
                alarm = detector.update(mean_mv)
                elapsed = time.monotonic() - start

                writer.writerow([
                    datetime.now().isoformat(timespec="milliseconds"),
                    f"{elapsed:.3f}",
                    f"{mean_mv:.6f}",
                    f"{med_mv:.6f}",
                    f"{min_mv:.6f}",
                    f"{max_mv:.6f}",
                    f"{delta:.6f}",
                    f"{detector.mu:.6f}",
                    f"{detector.sigma:.6f}",
                    f"{detector.pos:.6f}",
                    f"{detector.neg:.6f}",
                    alarm,
                    n,
                ])

                text = (
                    f"{elapsed:8.2f}s | "
                    f"avg {mean_mv:8.3f} mV | "
                    f"Δ {delta:+7.3f} | "
                    f"C+ {detector.pos:7.3f} | "
                    f"C- {detector.neg:7.3f}"
                )

                if alarm:
                    print(paint(text + f" | ANOMALY {alarm}", RED))
                    if args.reset_after_alert:
                        detector.reset()
                elif abs(delta) >= 2 * detector.sigma:
                    print(paint(text, YELLOW))
                else:
                    print(paint(text, GREEN))

                next_tick += interval_s

    except KeyboardInterrupt:
        print()
        print(paint("Stopped.", CYAN))
        print(f"CSV log: {log_path}")
        return 0
    except Exception as exc:
        print(paint(f"\nAcquisition error: {exc}", RED))
        print(f"Partial CSV log: {log_path}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
