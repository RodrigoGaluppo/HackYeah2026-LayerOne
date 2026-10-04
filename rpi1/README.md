# Pi1 — encrypted sensor transmitter

Host: `rpi1` / `192.168.0.197`
Node ID: `sensor-rpi1`
Role: Solar Panel 1 heartbeat transmitter. It emits signed,
ChaCha20-Poly1305-encrypted heartbeat packets over the transparent USB LoRa
adapter and does not generate timed or synthetic alerts.

## Install or refresh

From this folder on Pi1:

```bash
chmod +x install.sh
./install.sh
```

The service starts automatically and uses `/dev/ttyACM0` at `115200` baud.

## Operate

```bash
systemctl status layerone-node --no-pager
journalctl -u layerone-node -f
sudo systemctl restart layerone-node
```

For a foreground test, stop the service first so two processes do not contend for the serial port:

```bash
sudo systemctl stop layerone-node
python3 /home/russo/layerone/layerone.py node --config /home/russo/layerone/node.json
```

## Keys and enrollment

The node creates Ed25519 signing and X25519 exchange private keys under `/home/russo/layerone/keys/sensor-rpi1`. Never copy those private files off the node. `install.sh` prints the two public values required by Pi3 enrollment.

Back up the existing key directory before rebuilding Pi1. If new keys are generated, register the new public values on Pi3 using the command in `../pi3/README.md`.
