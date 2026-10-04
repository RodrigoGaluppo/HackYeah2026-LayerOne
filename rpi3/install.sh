#!/bin/sh
set -eu

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DEST=/home/russo/layerone

sudo apt-get update
sudo apt-get install -y python3-serial python3-cryptography python3-flask
sudo usermod -aG dialout russo

mkdir -p "$DEST/static" "$DEST/data" "$DEST/keys/server"
install -m 755 "$HERE/layerone.py" "$DEST/layerone.py"
install -m 755 "$HERE/init_server_keys.py" "$DEST/init_server_keys.py"
install -m 600 "$HERE/server.json" "$DEST/server.json"
install -m 644 "$HERE/static/index.html" "$DEST/static/index.html"
python3 "$DEST/init_server_keys.py"

sudo install -m 644 "$HERE/layerone-server.service" /etc/systemd/system/layerone-server.service
sudo install -m 644 "$HERE/layerone-dashboard.service" /etc/systemd/system/layerone-dashboard.service
sudo systemctl daemon-reload
sudo systemctl enable --now layerone-server.service layerone-dashboard.service

echo "Pi3 installed. Dashboard: http://192.168.0.198:8090/"
