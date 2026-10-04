# GPIO4 button → controlled side-channel anomaly

This firmware creates a simple side-channel detection demonstration:

| State | LED task | Workload |
|---|---|---|
| Baseline / armed | GPIO19 heartbeat: 1 s ON / 9 s OFF | Wi-Fi off; compute and flash workers idle |
| GPIO4 pressed | Rapid 12.5 Hz strobe for 8 seconds | Dual-core SHA-256, RAM churn, 24 bounded NVS writes, max-power AP, active scans, local UDP bursts |
| Recovery | GPIO19 OFF | Workload stopped; Wi-Fi disabled; next press is armed after button release |

The triggered phase emulates the power behavior of an unauthorized cryptomining/integrity-scanning process. It is not malware: it contains no exploit, persistence, propagation, credential access, or internet destination. UDP traffic stays on the ESP32's private `192.168.4.0/24` access point. Flash programming is capped at 24 writes per cycle to limit wear. The anomaly stops automatically after 8 seconds or when internal temperature reaches 75°C.

For the 4x4 membrane keypad, key `1` closes its R1/C1 matrix contacts. Connect
the key-1 row contact to **GPIO4 (D4)** and the key-1 column contact to
**GPIO18**. The firmware drives GPIO4 LOW and reads GPIO18 with `INPUT_PULLUP`,
so no keypad contact is connected to GND. Keep key `1` released while powering
or resetting the board, and release it before pressing it again.

## Run the live PC detector

Close OpenHantek, leave the button released for baseline learning, then run:

```bash
cd /home/russo/light_esp32_project/mvp_esp32_sidechannel/detector
python3 -u button_cusum_monitor.py
```

After `ARMED` appears, press the GPIO4 button. The monitor prints every sensor
window, raises a CUSUM alert, confirms recovery after the 8-second cycle, and
rearms automatically. Results are saved to `button_cusum_log.csv`.

## Observed result on the current bench setup

With CH1 connected to the ACS sensor output and 500 ms acquisition windows,
the rapid-strobe composite workload gave:

| Phase | Mean sensor output | Standard deviation |
|---|---:|---:|
| Baseline (known LED pulses excluded) | 108.444 mV | 0.198 mV |
| Aggressive composite workload | 119.871 mV | 1.082 mV |
| Recovery (LED off windows) | 108.447 mV | 0.168 mV |

The aggressive phase produced a sustained **+11.427 mV** shift with a
standardized effect size of about **14.32**. After the anomaly stopped, the
mean returned to within **+0.004 mV** of baseline. This version is therefore
well outside the learned baseline in the current bench configuration.

## Observe serial phase labels

```bash
/home/russo/bin/arduino-cli monitor --port /dev/ttyUSB0 --config baudrate=115200
```

## Expected labels

```text
[ARMED] Press GPIO4 to GND to run an 8-second anomaly cycle
[TRIGGER] GPIO4 button pressed — anomaly cycle started
[ANOMALY] Wi-Fi enabled at maximum TX power
[ANOMALY] hashes=... ram_passes=... flash_writes=... scans=... packets=... temp=...C
[RECOVERY] 8-second cycle complete; quiet baseline restored
```
