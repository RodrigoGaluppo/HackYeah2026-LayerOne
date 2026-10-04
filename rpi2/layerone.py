#!/usr/bin/env python3
"""LayerOne LoRa MVP: signed, encrypted node telemetry and command dashboard."""
import argparse
import base64
import datetime as dt
import json
import math
import os
import random
import sqlite3
import statistics
import struct
import subprocess
import threading
import time
import socket
from collections import deque
from pathlib import Path

import serial
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, x25519
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

MAGIC = b"L1"
VERSION = 1
K_HEARTBEAT = 1
K_ALERT = 2
HEADER = struct.Struct("!2sBBBBIII")
EVENT = struct.Struct("!BBIIiihhhH")
HEARTBEAT = struct.Struct("!BIhH")
NODE_NUMBERS = {"sensor-rpi1": 1, "sensor-rpi2": 2}
NODE_NAMES = {value: key for key, value in NODE_NUMBERS.items()}


def parse_sigrok_voltage_mv(line, expected_channel):
    line = line.strip()
    if not line or ":" not in line:
        return None
    channel, reading = line.split(":", 1)
    if channel.strip().upper() != expected_channel.upper():
        return None
    parts = reading.strip().split()
    if len(parts) < 2:
        return None
    try:
        value = float(parts[0])
    except ValueError:
        return None
    factors = {"V": 1000.0, "mV": 1.0, "uV": 0.001, "µV": 0.001, "nV": 0.000001}
    factor = factors.get(parts[1])
    return value * factor if factor is not None and math.isfinite(value) else None


def robust_center_sigma(values):
    center = statistics.median(values)
    mad = statistics.median(abs(value - center) for value in values)
    sigma = 1.4826 * mad
    if sigma <= 1e-12 and len(values) > 1:
        sigma = statistics.pstdev(values)
    return center, sigma


def acquire_scope_mean(config):
    samplerate = int(config.get("scope_samplerate", 100_000))
    capture_ms = float(config.get("scope_capture_ms", 20.0))
    sample_count = max(100, int(samplerate * capture_ms / 1000.0))
    rate = "%sk" % (samplerate // 1000) if samplerate % 1000 == 0 else str(samplerate)
    channel = config.get("scope_channel", "CH1")
    command = [
        "sigrok-cli", "--driver", config.get("scope_driver", "hantek-6xxx"),
        "--channels", channel, "--config", "samplerate=" + rate,
        "--samples", str(sample_count),
    ]
    result = subprocess.run(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        timeout=max(10.0, capture_ms / 1000.0 + 5.0), check=False,
        env=dict(os.environ, LC_ALL="C", LANG="C"),
    )
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout or "sigrok capture failed").strip())
    values = [parse_sigrok_voltage_mv(line, channel) for line in result.stdout.splitlines()]
    values = [value for value in values if value is not None]
    if not values:
        raise RuntimeError("sigrok returned no parseable %s samples" % channel)
    values.sort()
    cut = int(len(values) * float(config.get("scope_trim", 0.01)))
    trimmed = values[cut:-cut] if cut and cut * 2 < len(values) else values
    return statistics.fmean(trimmed)


def b64(value):
    return base64.b64encode(value).decode("ascii")


def unb64(value):
    return base64.b64decode(value.encode("ascii"))


def load_config(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def write_private(path, raw):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(raw)
    os.chmod(path, 0o600)


def private_raw(key):
    return key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())


def public_raw(key):
    return key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def load_or_create_node_keys(key_dir):
    key_dir = Path(key_dir)
    sign_path, exchange_path = key_dir / "signing.key", key_dir / "exchange.key"
    if not sign_path.exists():
        write_private(sign_path, private_raw(ed25519.Ed25519PrivateKey.generate()))
    if not exchange_path.exists():
        write_private(exchange_path, private_raw(x25519.X25519PrivateKey.generate()))
    signer = ed25519.Ed25519PrivateKey.from_private_bytes(sign_path.read_bytes())
    exchanger = x25519.X25519PrivateKey.from_private_bytes(exchange_path.read_bytes())
    return signer, exchanger


def day_number():
    return (dt.date.today() - dt.date(2020, 1, 1)).days


def session_key(private_key, public_key_raw, node_number, epoch):
    shared = private_key.exchange(x25519.X25519PublicKey.from_public_bytes(public_key_raw))
    info = b"LayerOne/LoRa/v1/" + bytes([node_number]) + struct.pack("!I", epoch)
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(shared)


def nonce(epoch, boot, counter):
    return struct.pack("!III", epoch, boot, counter)


def init_db(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS nodes (
          node_id TEXT PRIMARY KEY, node_number INTEGER UNIQUE NOT NULL,
          role TEXT NOT NULL, location_name TEXT NOT NULL, latitude REAL NOT NULL,
          longitude REAL NOT NULL, signing_public BLOB NOT NULL,
          exchange_public BLOB NOT NULL, status TEXT NOT NULL DEFAULT 'active',
          key_version INTEGER NOT NULL DEFAULT 1, last_seen TEXT, last_rssi REAL,
          last_snr REAL, last_counter INTEGER, accepted_packets INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS events (
          id INTEGER PRIMARY KEY AUTOINCREMENT, event_ref INTEGER NOT NULL,
          node_id TEXT NOT NULL, received_at TEXT NOT NULL, detected_at TEXT NOT NULL,
          event_type TEXT NOT NULL, severity TEXT NOT NULL, latitude REAL NOT NULL,
          longitude REAL NOT NULL, mean_mv REAL NOT NULL, baseline_mv REAL NOT NULL,
          delta_mv REAL NOT NULL, cusum REAL NOT NULL, packet_counter INTEGER NOT NULL,
          UNIQUE(node_id, event_ref)
        );
        CREATE TABLE IF NOT EXISTS packet_log (
          id INTEGER PRIMARY KEY AUTOINCREMENT, received_at TEXT NOT NULL,
          node_id TEXT, result TEXT NOT NULL, detail TEXT, rssi REAL, snr REAL
        );
        CREATE TABLE IF NOT EXISTS replay_guard (
          node_id TEXT NOT NULL, epoch INTEGER NOT NULL, boot INTEGER NOT NULL,
          counter INTEGER NOT NULL, received_at TEXT NOT NULL,
          PRIMARY KEY(node_id, epoch, boot, counter)
        );
    """)
    node_columns = {row[1] for row in con.execute("PRAGMA table_info(nodes)")}
    for name, sql_type in {
        "last_heartbeat": "TEXT", "last_epoch": "INTEGER", "last_boot": "INTEGER",
        "last_uptime": "INTEGER", "cpu_temp_c": "REAL", "supply_voltage_v": "REAL",
    }.items():
        if name not in node_columns:
            con.execute(f"ALTER TABLE nodes ADD COLUMN {name} {sql_type}")
    log_columns = {row[1] for row in con.execute("PRAGMA table_info(packet_log)")}
    for name, sql_type in {
        "packet_kind": "TEXT", "packet_counter": "INTEGER", "epoch": "INTEGER",
        "boot": "INTEGER", "key_version": "INTEGER",
    }.items():
        if name not in log_columns:
            con.execute(f"ALTER TABLE packet_log ADD COLUMN {name} {sql_type}")
    con.commit()
    return con


def iso_now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def log_packet(con, node_id, result, detail="", rssi=None, snr=None,
               packet_kind=None, packet_counter=None, epoch=None, boot=None, key_version=None):
    con.execute("""INSERT INTO packet_log(received_at,node_id,result,detail,rssi,snr,
                   packet_kind,packet_counter,epoch,boot,key_version) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (iso_now(), node_id, result, detail, rssi, snr, packet_kind,
                 packet_counter, epoch, boot, key_version))
    con.commit()


class ServerReceiver:
    def __init__(self, config):
        self.config = config
        self.db_path = config["database"]
        self.server_exchange = x25519.X25519PrivateKey.from_private_bytes(Path(config["server_exchange_key"]).read_bytes())
        self.serial_port = config["serial_port"]
        self.baud = config.get("baud", 9600)
        self.stop = threading.Event()

    def process_line(self, raw, rssi=None, snr=None):
        line = raw.strip()
        if not line.startswith(b"L1:"):
            return False
        try:
            packet = base64.b64decode(line[3:], validate=True)
            if len(packet) < HEADER.size + 16 + 64:
                raise ValueError("packet too short")
            header = packet[:HEADER.size]
            magic, version, kind, node_number, key_version, epoch, boot, counter = HEADER.unpack(header)
            if magic != MAGIC or version != VERSION or node_number not in NODE_NAMES:
                raise ValueError("unknown protocol header")
            node_id = NODE_NAMES[node_number]
            ciphertext, signature = packet[HEADER.size:-64], packet[-64:]
            con = init_db(self.db_path)
            row = con.execute("SELECT signing_public,exchange_public,status FROM nodes WHERE node_id=?", (node_id,)).fetchone()
            if not row or row[2] != "active":
                log_packet(con, node_id, "rejected", "unknown or inactive node", rssi, snr,
                           "unknown", counter, epoch, boot, key_version)
                con.close(); return False
            ed25519.Ed25519PublicKey.from_public_bytes(row[0]).verify(signature, header + ciphertext)
            key = session_key(self.server_exchange, row[1], node_number, epoch)
            plaintext = ChaCha20Poly1305(key).decrypt(nonce(epoch, boot, counter), ciphertext, header)
            try:
                con.execute("INSERT INTO replay_guard(node_id,epoch,boot,counter,received_at) VALUES(?,?,?,?,?)",
                            (node_id, epoch, boot, counter, iso_now()))
            except sqlite3.IntegrityError:
                log_packet(con, node_id, "duplicate", "replayed packet", rssi, snr,
                           "replay", counter, epoch, boot, key_version)
                con.close(); return False
            now = iso_now()
            con.execute("""UPDATE nodes SET last_seen=?,last_rssi=?,last_snr=?,last_counter=?,
                        last_epoch=?,last_boot=?,key_version=?,accepted_packets=accepted_packets+1
                        WHERE node_id=?""",
                        (now, rssi, snr, counter, epoch, boot, key_version, node_id))
            if kind == K_HEARTBEAT:
                if len(plaintext) != HEARTBEAT.size:
                    raise ValueError("invalid heartbeat")
                _, uptime, cpu_tenths, supply_mv = HEARTBEAT.unpack(plaintext)
                con.execute("""UPDATE nodes SET last_heartbeat=?,last_uptime=?,cpu_temp_c=?,
                            supply_voltage_v=? WHERE node_id=?""",
                            (now, uptime, cpu_tenths / 10, supply_mv / 1000, node_id))
                log_packet(con, node_id, "accepted", "heartbeat", rssi, snr,
                           "heartbeat", counter, epoch, boot, key_version)
            elif kind == K_ALERT:
                if len(plaintext) != EVENT.size:
                    raise ValueError("invalid alert")
                _, severity, detected, event_ref, lat, lon, mean, baseline, delta, cusum = EVENT.unpack(plaintext)
                severities = {1: "notice", 2: "elevated", 3: "critical"}
                con.execute("""INSERT OR IGNORE INTO events(event_ref,node_id,received_at,detected_at,event_type,severity,latitude,longitude,mean_mv,baseline_mv,delta_mv,cusum,packet_counter)
                               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (event_ref, node_id, now, dt.datetime.fromtimestamp(detected, dt.timezone.utc).isoformat(timespec="seconds"),
                             "Electrical deviation", severities.get(severity, "notice"), lat / 1_000_000, lon / 1_000_000,
                             mean / 10, baseline / 10, delta / 10, cusum / 100, counter))
                log_packet(con, node_id, "accepted", "alert", rssi, snr,
                           "alert", counter, epoch, boot, key_version)
            else:
                raise ValueError("unknown message kind")
            con.commit(); con.close(); return True
        except (ValueError, InvalidSignature, struct.error) as exc:
            con = init_db(self.db_path)
            log_packet(con, None, "rejected", str(exc), rssi, snr)
            con.close(); return False

    def serial_loop(self):
        while not self.stop.is_set():
            try:
                with serial.Serial(self.serial_port, self.baud, timeout=1) as radio:
                    while not self.stop.is_set():
                        line = radio.readline()
                        if line:
                            self.process_line(line)
            except serial.SerialException:
                time.sleep(3)

    def udp_loop(self):
        port = self.config.get("test_udp_port")
        if not port:
            return
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind((self.config.get("test_udp_bind", "0.0.0.0"), port))
        sock.settimeout(1)
        while not self.stop.is_set():
            try:
                raw, _ = sock.recvfrom(1024)
                self.process_line(raw)
            except socket.timeout:
                pass


class SensorNode:
    def __init__(self, config):
        self.config = config
        self.node_id = config["node_id"]
        self.node_number = NODE_NUMBERS[self.node_id]
        self.signer, self.exchanger = load_or_create_node_keys(config["key_dir"])
        self.server_exchange_public = unb64(config["server_exchange_public"])
        self.serial_port = config["serial_port"]
        self.baud = config.get("baud", 9600)
        self.boot = random.SystemRandom().randrange(1, 2**32)
        self.counter = 0
        self.secure_random = random.SystemRandom()
        self.udp_target = None
        if config.get("test_udp_host"):
            self.udp_target = (config["test_udp_host"], config.get("test_udp_port", 9170))

    def packet(self, kind, payload):
        self.counter += 1
        epoch = day_number()
        header = HEADER.pack(MAGIC, VERSION, kind, self.node_number, 1, epoch, self.boot, self.counter)
        key = session_key(self.exchanger, self.server_exchange_public, self.node_number, epoch)
        ciphertext = ChaCha20Poly1305(key).encrypt(nonce(epoch, self.boot, self.counter), payload, header)
        signature = self.signer.sign(header + ciphertext)
        return b"L1:" + b64(header + ciphertext + signature).encode("ascii") + b"\n"

    def heartbeat(self):
        uptime = int(time.monotonic())
        try:
            temperature = int(round(float(Path("/sys/class/thermal/thermal_zone0/temp").read_text().strip()) / 100.0))
        except (OSError, ValueError):
            temperature = 0
        # No ADC is attached to the Pi supply rail, so report unavailable as zero.
        supply = 0
        return self.packet(K_HEARTBEAT, HEARTBEAT.pack(1, uptime, temperature, supply))

    def alert(self, mean_mv, baseline_mv, delta_mv, cusum_mv):
        cfg = self.config
        mean = max(-32768, min(32767, int(round(mean_mv * 10))))
        baseline = max(-32768, min(32767, int(round(baseline_mv * 10))))
        delta = max(-32768, min(32767, int(round(delta_mv * 10))))
        cusum = max(0, min(65535, int(round(abs(cusum_mv) * 100))))
        severity = 3 if abs(delta_mv) >= float(cfg.get("critical_delta_mv", 5.0)) else 2
        return self.packet(K_ALERT, EVENT.pack(1, severity, int(time.time()), self.secure_random.randrange(1, 2**32),
                                                 int(cfg["latitude"] * 1_000_000), int(cfg["longitude"] * 1_000_000),
                                                 mean, baseline, delta, cusum))

    def learn_baseline(self):
        interval = float(self.config.get("scope_interval_ms", 500.0)) / 1000.0
        seconds = float(self.config.get("baseline_seconds", 8.0))
        count = max(4, int(math.ceil(seconds / interval)))
        values = []
        print("CUSUM calibration: collecting %s live scope windows" % count, flush=True)
        for index in range(count):
            started = time.monotonic()
            value = acquire_scope_mean(self.config)
            values.append(value)
            print("baseline %02d/%02d mean=%.3f mV" % (index + 1, count, value), flush=True)
            time.sleep(max(0.0, interval - (time.monotonic() - started)))
        center, measured_sigma = robust_center_sigma(values)
        sigma = max(measured_sigma, float(self.config.get("sigma_floor_mv", 0.10)))
        print("CUSUM armed: baseline=%.3f mV sigma=%.3f mV" % (center, sigma), flush=True)
        return center, sigma

    def run(self):
        heartbeat_every = self.config.get("heartbeat_seconds", 15)
        interval = float(self.config.get("scope_interval_ms", 500.0)) / 1000.0
        baseline, sigma = self.learn_baseline()
        allowance = float(self.config.get("k_sigma", 0.5)) * sigma
        threshold = float(self.config.get("h_sigma", 5.0)) * sigma
        trigger_delta = max(float(self.config.get("trigger_sigma", 3.0)) * sigma,
                            float(self.config.get("trigger_floor_mv", 1.0)))
        recovery_band = max(3.0 * sigma, float(self.config.get("recovery_floor_mv", 0.5)))
        vote_window = int(self.config.get("vote_window", 5))
        votes_required = int(self.config.get("votes_required", 3))
        recovery_required = int(self.config.get("recovery_windows", 6))
        votes = deque(maxlen=vote_window)
        positive = negative = 0.0
        recovery_run = 0
        state = "ARMED"
        last_heartbeat = 0.0
        while True:
            try:
                with serial.Serial(self.serial_port, self.baud, timeout=1, write_timeout=3) as radio:
                    while True:
                        started = time.monotonic()
                        mean_mv = acquire_scope_mean(self.config)
                        delta = mean_mv - baseline
                        positive = max(0.0, positive + delta - allowance)
                        negative = max(0.0, negative - delta - allowance)

                        if state == "ARMED":
                            votes.append(1 if delta >= trigger_delta else -1 if delta <= -trigger_delta else 0)
                            sustained = max(votes.count(1), votes.count(-1)) >= votes_required
                            if sustained and max(positive, negative) >= threshold:
                                message = self.alert(mean_mv, baseline, delta, max(positive, negative))
                                radio.write(message); radio.flush()
                                state = "ANOMALY"
                                recovery_run = 0
                                print("REAL CUSUM alert sent counter=%s mean=%.3f baseline=%.3f delta=%+.3f cusum=%.3f" %
                                      (self.counter, mean_mv, baseline, delta, max(positive, negative)), flush=True)
                            elif len(votes) == vote_window and not sustained:
                                positive = negative = 0.0
                        else:
                            recovery_run = recovery_run + 1 if abs(delta) <= recovery_band else 0
                            if recovery_run >= recovery_required:
                                state = "ARMED"
                                positive = negative = 0.0
                                recovery_run = 0
                                votes.clear()
                                print("CUSUM recovered and rearmed", flush=True)

                        now = time.monotonic()
                        if now - last_heartbeat >= heartbeat_every:
                            message = self.heartbeat()
                            radio.write(message); radio.flush()
                            last_heartbeat = now
                            if self.udp_target:
                                socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(message, self.udp_target)
                            print("heartbeat sent counter=%s bytes=%s" % (self.counter, len(message)), flush=True)
                        print("scope mean=%.3f delta=%+.3f cusum+=%.3f cusum-=%.3f state=%s" %
                              (mean_mv, delta, positive, negative, state), flush=True)
                        time.sleep(max(0.0, interval - (time.monotonic() - started)))
            except (serial.SerialException, RuntimeError, subprocess.TimeoutExpired) as exc:
                print("sensor pipeline unavailable: %s" % exc, flush=True)
                time.sleep(3)


def bootstrap(args):
    signer, exchanger = load_or_create_node_keys(args.key_dir)
    print(json.dumps({
        "signing_public": b64(public_raw(signer.public_key())),
        "exchange_public": b64(public_raw(exchanger.public_key()))
    }))


def register(args):
    con = init_db(args.database)
    number = NODE_NUMBERS[args.node_id]
    con.execute("""INSERT INTO nodes(node_id,node_number,role,location_name,latitude,longitude,signing_public,exchange_public,status)
                   VALUES(?,?,?,?,?,?,?,?, 'active')
                   ON CONFLICT(node_id) DO UPDATE SET role=excluded.role,location_name=excluded.location_name,latitude=excluded.latitude,longitude=excluded.longitude,signing_public=excluded.signing_public,exchange_public=excluded.exchange_public,status='active'""",
                (args.node_id, number, "sensor", args.location, args.latitude, args.longitude,
                 unb64(args.signing_public), unb64(args.exchange_public)))
    con.commit(); con.close()


def make_app(config):
    from flask import Flask, jsonify, send_from_directory
    app = Flask(__name__, static_folder=str(Path(__file__).with_name("static")))
    db_path = config["database"]
    init_db(db_path).close()

    @app.route("/")
    def dashboard():
        return send_from_directory(app.static_folder, "index.html")

    @app.route("/api/overview")
    def overview():
        con = sqlite3.connect(db_path); con.row_factory = sqlite3.Row
        nodes = [dict(row) for row in con.execute("""SELECT node_id,location_name,latitude,longitude,status,
                    last_seen,last_heartbeat,last_counter,last_epoch,last_boot,last_uptime,cpu_temp_c,
                    supply_voltage_v,key_version,accepted_packets FROM nodes ORDER BY node_id""")]
        events = [dict(row) for row in con.execute("SELECT * FROM events ORDER BY id DESC LIMIT 50")]
        logs = [dict(row) for row in con.execute("""SELECT id,received_at,node_id,result,detail,packet_kind,
                    packet_counter,epoch,boot,key_version FROM packet_log ORDER BY id DESC LIMIT 80""")]
        total = con.execute("SELECT COUNT(*) FROM packet_log WHERE result='accepted'").fetchone()[0]
        rejected = con.execute("SELECT COUNT(*) FROM packet_log WHERE result IN ('rejected','duplicate')").fetchone()[0]
        con.close()
        return jsonify({"now": iso_now(), "nodes": nodes, "events": events, "logs": logs,
                        "accepted": total, "rejected": rejected,
                        "receiver": {"serial_port": config.get("serial_port", "/dev/ttyUSB0"),
                                     "baud": config.get("baud", 115200), "rf_metadata": False,
                                     "protocol": "L1-v1", "cipher": "ChaCha20-Poly1305",
                                     "signature": "Ed25519"}})
    return app


def run_server(config):
    receiver = ServerReceiver(config)
    if config.get("test_udp_port"):
        thread = threading.Thread(target=receiver.serial_loop, daemon=True)
        thread.start()
        receiver.udp_loop()
    else:
        receiver.serial_loop()


def main():
    parser = argparse.ArgumentParser()
    subs = parser.add_subparsers(dest="command", required=True)
    boot = subs.add_parser("bootstrap"); boot.add_argument("--key-dir", required=True); boot.set_defaults(fn=bootstrap)
    reg = subs.add_parser("register"); reg.add_argument("--database", required=True); reg.add_argument("--node-id", required=True, choices=NODE_NUMBERS); reg.add_argument("--location", required=True); reg.add_argument("--latitude", type=float, required=True); reg.add_argument("--longitude", type=float, required=True); reg.add_argument("--signing-public", required=True); reg.add_argument("--exchange-public", required=True); reg.set_defaults(fn=register)
    node = subs.add_parser("node"); node.add_argument("--config", required=True); node.set_defaults(fn=lambda a: SensorNode(load_config(a.config)).run())
    server = subs.add_parser("server"); server.add_argument("--config", required=True); server.set_defaults(fn=lambda a: run_server(load_config(a.config)))
    dash = subs.add_parser("dashboard"); dash.add_argument("--config", required=True); dash.add_argument("--port", type=int, default=8090); dash.set_defaults(fn=lambda a: make_app(load_config(a.config)).run(host="0.0.0.0", port=a.port, debug=False))
    args = parser.parse_args(); args.fn(args)


if __name__ == "__main__":
    main()
