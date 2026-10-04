# Pi2 — encrypted sensor transmitter

Host: `rpi2` / `192.168.0.196`
Node ID: `sensor-rpi2`
Role: acquire the Hantek 6022BE CH1 signal from the ACS sensor, learn a quiet
baseline, detect sustained deviations with a two-sided CUSUM, and emit signed,
ChaCha20-Poly1305-encrypted alerts over the transparent USB LoRa adapter.

Alerts contain the measured mean, learned baseline, measured delta, and live
CUSUM score. There is no timer-driven or generated alert data.

## Install or refresh

```bash
chmod +x install.sh
./install.sh
```

## Operate

```bash
systemctl status layerone-node --no-pager
journalctl -u layerone-node -f
sudo systemctl restart layerone-node
```

Foreground test (stop the service first):

```bash
sudo systemctl stop layerone-node
python3 /home/russo/layerone/layerone.py node --config /home/russo/layerone/node.json
```

## Keys and enrollment

Private signing/exchange keys remain under `/home/russo/layerone/keys/sensor-rpi2`. Never send them to Pi3 or Pi4. `install.sh` prints only the public enrollment values. Preserve the key directory across upgrades; if it changes, re-enroll the public keys on Pi3.
