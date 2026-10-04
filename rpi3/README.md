# Pi3 — Central Command receiver and dashboard

Host: `pi3` / `192.168.0.198`
Role: read `/dev/ttyACM0` at `115200`, verify Ed25519 signatures, derive the daily X25519/HKDF key, authenticate/decrypt ChaCha20-Poly1305 payloads, apply replay protection, persist telemetry, and serve the dashboard.

## Install or refresh

```bash
chmod +x install.sh
./install.sh
```

The initializer preserves an existing server private key. On a fresh install it prints a new Base64 public exchange key; place that value in `server_exchange_public` inside both node configurations before starting Pi1/Pi2.

## Operate

```bash
systemctl status layerone-server layerone-dashboard --no-pager
journalctl -u layerone-server -f
curl -s http://127.0.0.1:8090/api/overview
```

Dashboard: <http://192.168.0.198:8090/>

## Enroll a node

Run the node's `bootstrap` command and copy only its `signing_public` and `exchange_public` values. Then run on Pi3:

```bash
python3 /home/russo/layerone/layerone.py register \
  --database /home/russo/layerone/data/central-command.db \
  --node-id sensor-rpi1 \
  --location "Packaging hall · Line A" \
  --latitude 52.2297 --longitude 21.0122 \
  --signing-public 'PASTE_PI1_SIGNING_PUBLIC' \
  --exchange-public 'PASTE_PI1_EXCHANGE_PUBLIC'
```

For Pi2, change the node to `sensor-rpi2`, location to `Test cell · Line B`, coordinates to `52.2321 / 21.0180`, and use Pi2's public values.

Restart the receiver after enrollment:

```bash
sudo systemctl restart layerone-server
```

Never place node private keys in this folder. Preserve `/home/russo/layerone/keys/server/exchange.key` and the SQLite database during upgrades.
