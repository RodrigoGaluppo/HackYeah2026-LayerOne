#!/bin/sh
set -eu

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DEST=/home/russo/layerone

sudo apt-get update
sudo apt-get install -y python3-serial python3-cryptography
sudo usermod -aG dialout russo

mkdir -p "$DEST/keys/sensor-rpi1"
install -m 755 "$HERE/layerone.py" "$DEST/layerone.py"
install -m 600 "$HERE/node.json" "$DEST/node.json"
sudo install -m 644 "$HERE/layerone-node.service" /etc/systemd/system/layerone-node.service
sudo systemctl daemon-reload
sudo systemctl enable --now layerone-node.service

echo "Pi1 installed. Inspect with: journalctl -u layerone-node -f"
echo "Public enrollment record:"
python3 "$DEST/layerone.py" bootstrap --key-dir "$DEST/keys/sensor-rpi1"
