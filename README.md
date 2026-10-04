# LayerOne

LayerOne is a physical side-channel security MVP for detecting abnormal
electrical behavior in an ESP32 workload and reporting the result through an
independent, authenticated LoRa path.

The demonstration uses two Raspberry Pi sensor nodes and one Raspberry Pi
receiver:

- **Solar Panel 1 / Raspberry Pi 1** captures live host health information and
  emits authenticated heartbeat packets.
- **Solar Panel 2 / Raspberry Pi 2** captures live Hantek/ACS measurements,
  learns a baseline, runs a two-sided CUSUM detector, and emits heartbeats plus
  real anomaly alerts.
- **Raspberry Pi 3** receives LoRa frames, verifies signatures, decrypts the
  payloads, rejects replays, stores accepted telemetry, and hosts the dashboard.

No synthetic alert generator is required. The included ESP32 firmware provides
a bounded, controlled workload for demonstrating an observable physical signal.

## Contents

```text
LayerOne/
├── README.md
├── .gitignore
├── esp32/
│   └── BlinkWifiAnomalyLab/
│       ├── BlinkWifiAnomalyLab.ino
│       └── README.md
├── detector/
│   ├── acs_scope_cusum.py
│   └── button_cusum_monitor.py
├── rpi1/
│   ├── README.md
│   ├── install.sh
│   ├── layerone-node.service
│   ├── layerone.py
│   └── node.json
├── rpi2/
│   ├── README.md
│   ├── install.sh
│   ├── layerone-node.service
│   ├── layerone.py
│   └── node.json
└── rpi3/
    ├── README.md
    ├── init_server_keys.py
    ├── install.sh
    ├── layerone-dashboard.service
    ├── layerone-server.service
    ├── layerone.py
    ├── server.json
    └── static/index.html
```

Runtime databases, private keys, logs, CSV captures, Python caches, and firmware
build products are intentionally excluded from this repository.

## Solution design

### System architecture

```mermaid
flowchart LR
    subgraph Plant[Physical process / demonstration load]
        ESP[ESP32 workload target]
        ACS[ACS current sensor]
        SCOPE[Hantek 6022BE CH1]
        ESP -->|supply current| ACS
        ACS -->|analog voltage| SCOPE
    end

    subgraph P1[Solar Panel 1 · Raspberry Pi 1]
        H1[Live uptime and CPU temperature]
        N1[Heartbeat node]
        H1 --> N1
    end

    subgraph P2[Solar Panel 2 · Raspberry Pi 2]
        SIG[sigrok acquisition]
        BASE[Robust baseline]
        CUSUM[Two-sided CUSUM]
        N2[Secure sensor node]
        SIG --> BASE --> CUSUM --> N2
    end

    SCOPE -->|USB| SIG
    N1 -->|signed + encrypted LoRa| RADIO[Transparent LoRa data plane]
    N2 -->|signed + encrypted LoRa| RADIO
    RADIO --> RX[Pi3 secure receiver]
    RX --> DB[(SQLite event store)]
    DB --> UI[LayerOne dashboard :8090]
```

### Network and trust boundaries

The management LAN is used for installation, SSH, and viewing the dashboard.
Sensor telemetry uses the independent LoRa serial path. The USB LoRa adapters
are transparent serial bridges; they do not provide the cryptographic trust.

```mermaid
flowchart TB
    subgraph LAN[Management LAN · 192.168.0.0/24]
        ADMIN[Operator workstation]
        PI1LAN[Solar Panel 1\n192.168.0.197]
        PI2LAN[Solar Panel 2\n192.168.0.196]
        PI3LAN[Pi3 Central Command\n192.168.0.198:8090]
        ADMIN -. SSH .-> PI1LAN
        ADMIN -. SSH .-> PI2LAN
        ADMIN -->|HTTPS/HTTP dashboard| PI3LAN
    end

    subgraph LORA[Independent LoRa serial data plane]
        PI1RF[Pi1 LoRa TX]
        PI2RF[Pi2 LoRa TX]
        PI3RF[Pi3 LoRa RX]
        PI1RF --> PI3RF
        PI2RF --> PI3RF
    end

    PI1LAN --- PI1RF
    PI2LAN --- PI2RF
    PI3RF --- PI3LAN
```

Default lab addresses are examples. Use DHCP reservations or static addresses
appropriate for the deployment network.

| Component | Default management address | Data interface | Role |
|---|---:|---|---|
| Solar Panel 1 | `192.168.0.197` | LoRa on `/dev/ttyACM0` | Live heartbeat sensor |
| Solar Panel 2 | `192.168.0.196` | Hantek USB + LoRa on `/dev/ttyACM0` | Live electrical/CUSUM sensor |
| Pi3 | `192.168.0.198` | LoRa on `/dev/ttyACM0` | Receiver, database, dashboard |
| ESP32 | USB serial; optional AP | GPIO/physical current path | Controlled workload target |

### Raspberry Pi responsibilities

```mermaid
flowchart LR
    P1[Solar Panel 1]
    P2[Solar Panel 2]
    P3[Pi3 Central Command]

    P1 --> P1A[Read live uptime]
    P1 --> P1B[Read live CPU temperature]
    P1 --> P1C[Create secure heartbeat]

    P2 --> P2A[Acquire Hantek CH1 windows]
    P2 --> P2B[Estimate baseline and noise]
    P2 --> P2C[Run CUSUM + voting]
    P2 --> P2D[Create heartbeat or measured alert]

    P3 --> P3A[Verify Ed25519 signature]
    P3 --> P3B[Derive daily session key]
    P3 --> P3C[Decrypt ChaCha20-Poly1305]
    P3 --> P3D[Apply replay guard]
    P3 --> P3E[Persist and visualize]
```

Solar Panel 1 reports `0` for supply voltage because no voltage ADC is attached;
zero means **unavailable**, not a measured zero-volt rail. Solar Panel 2 does the
same for Pi supply voltage while separately reporting live ACS measurements in
alert payloads.

## Cryptographic protocol

Each node owns two private keys that remain on that node:

- an **Ed25519 signing key** for packet authenticity;
- an **X25519 exchange key** for deriving encryption keys with Pi3.

Pi3 owns its X25519 private key. Only public keys are exchanged during
enrollment.

For each UTC day, both sides independently derive the same 256-bit key:

```text
shared_secret = X25519(node_private, server_public)
session_key   = HKDF-SHA256(
    shared_secret,
    info = "LayerOne/LoRa/v1/" || node_number || day_epoch
)
```

Payloads use ChaCha20-Poly1305 authenticated encryption. The 96-bit nonce is:

```text
nonce = day_epoch (32 bits) || boot_id (32 bits) || packet_counter (32 bits)
```

The header is authenticated as AEAD associated data. The node then signs the
header and ciphertext with Ed25519. Pi3 accepts a packet only after signature
verification, decryption/tag verification, node-status validation, and replay
checking.

```mermaid
sequenceDiagram
    participant N as Sensor node
    participant L as Transparent LoRa link
    participant R as Pi3 receiver
    participant D as SQLite/dashboard

    N->>N: Read live measurement
    N->>N: Derive daily X25519/HKDF key
    N->>N: Encrypt with ChaCha20-Poly1305
    N->>N: Sign header + ciphertext (Ed25519)
    N->>L: L1:Base64(packet)
    L->>R: Transparent serial frame
    R->>R: Resolve enrolled node
    R->>R: Verify Ed25519 signature
    R->>R: Derive daily session key
    R->>R: Verify tag and decrypt
    R->>R: Reject duplicate epoch/boot/counter
    R->>D: Commit verified heartbeat or alert
    D-->>D: Green heartbeat or red alert UI
```

Security properties:

- confidentiality and integrity from ChaCha20-Poly1305;
- sender authenticity from Ed25519;
- forward-separated daily keys through HKDF context;
- nonce uniqueness from day, random boot ID, and monotonic packet counter;
- replay rejection through the Pi3 SQLite replay guard.

## Physical detection and CUSUM mathematics

The Hantek samples the ACS sensor output on CH1. Each acquisition window is
converted to millivolts and reduced to a 1% trimmed mean, which limits the
influence of isolated capture spikes.

### Baseline estimation

During startup, Solar Panel 2 captures quiet windows while the ESP32 is in its
normal state. The baseline center is the median:

```math
\mu = \operatorname{median}(x_1, x_2, \ldots, x_n)
```

Noise is estimated robustly from the median absolute deviation:

```math
\operatorname{MAD} = \operatorname{median}(|x_i - \mu|)
```

```math
\sigma = \max(1.4826 \cdot \operatorname{MAD},\ \sigma_{floor})
```

The default noise floor is `0.10 mV`.

### Two-sided CUSUM

For each new window `x_t`, the detector accumulates positive and negative
deviation separately:

```math
S_t^+ = \max(0,\ S_{t-1}^+ + (x_t - \mu) - k)
```

```math
S_t^- = \max(0,\ S_{t-1}^- + (\mu - x_t) - k)
```

with:

```math
k = k_{\sigma}\sigma, \qquad h = h_{\sigma}\sigma
```

Defaults are `k_sigma = 0.5` and `h_sigma = 5.0`. A cumulative excursion must
cross `h`, but threshold crossing alone does not create an alert.

### Sustained-window voting

The detector also requires at least three of the latest five windows to exceed:

```math
\Delta_{trigger} = \max(3\sigma,\ 5.0\text{ mV})
```

This rejects the ESP32's normal one-second LED heartbeat, which typically
affects only two 500 ms observation windows. A real alert therefore requires
both cumulative CUSUM evidence and a sustained physical shift.

After an alert, six consecutive readings inside the recovery band re-arm the
detector:

```math
|x_t - \mu| \leq \max(3\sigma,\ 0.5\text{ mV})
```

```mermaid
stateDiagram-v2
    [*] --> Calibrating
    Calibrating --> Armed: baseline and sigma learned
    Armed --> Armed: transient / insufficient votes
    Armed --> Anomaly: CUSUM > h and 3-of-5 sustained votes
    Anomaly --> Anomaly: signal outside recovery band
    Anomaly --> Armed: 6 consecutive recovery windows
```

The transmitted alert contains the real measured mean, learned baseline,
measured delta, CUSUM score, detection timestamp, event reference, and node
coordinates. Pi3 does not fabricate these values.

## Hardware

Minimum lab hardware:

- 1 ESP32 development board;
- 1 4x4 membrane keypad or momentary contact;
- 1 LED and 330-ohm resistor if the board does not provide a suitable LED;
- 1 ACS current sensor appropriate for the measured current range;
- 1 Hantek 6022BE oscilloscope;
- 3 Raspberry Pi systems;
- 3 compatible transparent USB LoRa serial adapters (two transmitters, one
  receiver), configured to the same radio parameters;
- isolated, current-limited bench power and suitable USB cables.

### ESP32 keypad connections

For keypad key `1`:

| Signal | ESP32 pin |
|---|---:|
| Key-1 row contact | GPIO4 |
| Key-1 column contact | GPIO18 |
| LED / scope marker | GPIO19 |

The firmware drives GPIO4 low and reads GPIO18 with `INPUT_PULLUP`. Do not wire
the keypad contact directly to ground. Keep the key released while booting.

### Measurement path

```mermaid
flowchart LR
    PSU[Current-limited supply] --> ACSIN[ACS current path]
    ACSIN --> ESP[ESP32 load]
    ACSOUT[ACS analog output] --> CH1[Hantek CH1]
    CH1 --> USB[USB to Solar Panel 2]
    ESP --> GPIO19[GPIO19 visual/trigger marker]
```

Follow the ACS and Hantek manufacturers' voltage, current, grounding, and
isolation limits. Do not connect oscilloscope ground to an unsafe potential.

## Software prerequisites

The install scripts target Debian/Raspberry Pi OS and assume the deployment user
is `russo` with `/home/russo/layerone` as the runtime directory. If another user
is used, update the JSON files, install scripts, and systemd units first.

Packages installed by the scripts include:

- Python 3;
- `python3-serial`;
- `python3-cryptography`;
- `python3-flask` on Pi3;
- `sigrok-cli` and `sigrok-firmware-fx2lafw` on Solar Panel 2.

The ESP32 build requires Arduino CLI with the Espressif ESP32 board package.

## Fresh deployment

Deploy Pi3 first, then the two sensor nodes. This order ensures the Pi3 public
exchange key is available before node configuration.

### 1. Configure serial devices

On each Pi, identify the USB device:

```bash
lsusb
ls -l /dev/ttyACM* /dev/ttyUSB*
```

The example configs use `/dev/ttyACM0` at `115200`. Update `serial_port` if the
adapter enumerates differently. For permanent installations, create a udev
rule and use a stable symlink rather than relying on enumeration order.

### 2. Install Pi3 Central Command

Copy `rpi3/` to Pi3, then run:

```bash
cd rpi3
chmod +x install.sh
./install.sh
```

The installer creates or preserves:

```text
/home/russo/layerone/keys/server/exchange.key
```

Print the Pi3 public exchange key:

```bash
python3 /home/russo/layerone/init_server_keys.py
```

Copy this Base64 public value into `server_exchange_public` in both
`rpi1/node.json` and `rpi2/node.json`. Never copy the Pi3 private key.
The packaged JSON intentionally contains a placeholder and will not start a
node successfully until this value is replaced.

Check Pi3:

```bash
systemctl status layerone-server layerone-dashboard --no-pager
journalctl -u layerone-server -f
curl -s http://127.0.0.1:8090/api/overview
```

### 3. Install Solar Panel 1

Copy `rpi1/` to Raspberry Pi 1 after updating the Pi3 public key:

```bash
cd rpi1
chmod +x install.sh
./install.sh
```

The node creates private Ed25519 and X25519 keys under:

```text
/home/russo/layerone/keys/sensor-rpi1/
```

The installer prints the two public values needed for enrollment. To print them
again:

```bash
python3 /home/russo/layerone/layerone.py bootstrap \
  --key-dir /home/russo/layerone/keys/sensor-rpi1
```

Verify live heartbeat transmission:

```bash
systemctl status layerone-node --no-pager
journalctl -u layerone-node -f
```

Expected output includes:

```text
heartbeat sent counter=... bytes=...
```

### 4. Install Solar Panel 2

Connect the Hantek and LoRa adapter. Confirm the Hantek before installation:

```bash
lsusb
sigrok-cli --scan
sigrok-cli --driver hantek-6xxx --show
```

Copy `rpi2/` to Raspberry Pi 2 after updating the Pi3 public key:

```bash
cd rpi2
chmod +x install.sh
./install.sh
```

The installer adds the user to `dialout` and `plugdev`. A logout/login or reboot
may be required for new group membership to apply.

Print the public enrollment values again if required:

```bash
python3 /home/russo/layerone/layerone.py bootstrap \
  --key-dir /home/russo/layerone/keys/sensor-rpi2
```

Watch calibration and live acquisition:

```bash
journalctl -u layerone-node -f
```

Expected startup sequence:

```text
CUSUM calibration: collecting 16 live scope windows
baseline 01/16 mean=... mV
...
CUSUM armed: baseline=... mV sigma=... mV
heartbeat sent counter=... bytes=...
scope mean=... delta=... cusum+=... cusum-=... state=ARMED
```

Keep the ESP32 in its quiet state throughout calibration.

### 5. Enroll both nodes on Pi3

Use only the public values printed by each node.

Solar Panel 1:

```bash
python3 /home/russo/layerone/layerone.py register \
  --database /home/russo/layerone/data/central-command.db \
  --node-id sensor-rpi1 \
  --location "Solar Panel 1" \
  --latitude 52.2297 \
  --longitude 21.0122 \
  --signing-public 'PASTE_PI1_ED25519_PUBLIC_KEY' \
  --exchange-public 'PASTE_PI1_X25519_PUBLIC_KEY'
```

Solar Panel 2:

```bash
python3 /home/russo/layerone/layerone.py register \
  --database /home/russo/layerone/data/central-command.db \
  --node-id sensor-rpi2 \
  --location "Solar Panel 2" \
  --latitude 52.2321 \
  --longitude 21.0180 \
  --signing-public 'PASTE_PI2_ED25519_PUBLIC_KEY' \
  --exchange-public 'PASTE_PI2_X25519_PUBLIC_KEY'
```

Restart the receiver:

```bash
sudo systemctl restart layerone-server
```

### 6. Open the dashboard

```text
http://192.168.0.198:8090/
```

Normal behavior:

- a fresh heartbeat is green;
- an offline node is gray;
- red is reserved for a real accepted alert;
- the audit list shows verified heartbeat packets;
- an incoming CUSUM alert opens a prominent potential-cyberattack dialog with
  measured value, baseline, delta, CUSUM score, source, severity, and time.

`heartbeat_only_nodes` in `rpi3/server.json` can enforce a heartbeat-only policy
for selected nodes. The supplied configuration applies this to `sensor-rpi1`.

## Build and flash the ESP32 workload target

Install the ESP32 Arduino core, then compile:

```bash
arduino-cli compile \
  --fqbn esp32:esp32:esp32 \
  esp32/BlinkWifiAnomalyLab
```

Find the board port:

```bash
arduino-cli board list
```

Upload, replacing the port if necessary:

```bash
arduino-cli upload \
  --fqbn esp32:esp32:esp32 \
  --port /dev/ttyUSB0 \
  esp32/BlinkWifiAnomalyLab
```

Monitor serial labels:

```bash
arduino-cli monitor \
  --port /dev/ttyUSB0 \
  --config baudrate=115200
```

The firmware starts in a safe baseline state. Press keypad key `1` once to run
one bounded eight-second anomaly cycle. The cycle stops automatically or when
the internal temperature reaches 75 °C.

## Execute the end-to-end demonstration

1. Power the ESP32, Hantek, LoRa adapters, and all three Pis.
2. Confirm Pi3 receiver and dashboard services are active.
3. Confirm Solar Panel 1 produces verified heartbeats.
4. Keep the ESP32 quiet while Solar Panel 2 calibrates.
5. Wait for `CUSUM armed` in the Solar Panel 2 journal.
6. Open the Pi3 dashboard and confirm both nodes are green.
7. Press keypad key `1` once.
8. Observe a sustained electrical shift in the Solar Panel 2 journal.
9. Confirm Pi3 accepts the signed/encrypted alert.
10. Inspect the red dashboard popup and its measured fields.
11. Wait for six recovery windows and confirm the detector re-arms.

```mermaid
sequenceDiagram
    actor O as Operator
    participant E as ESP32
    participant P2 as Solar Panel 2
    participant P3 as Pi3
    participant UI as Dashboard

    O->>E: Press keypad 1
    E->>E: Run bounded 8 s workload
    P2->>P2: Observe sustained ACS shift
    P2->>P2: CUSUM + 3-of-5 vote trigger
    P2->>P3: Signed/encrypted alert over LoRa
    P3->>P3: Verify, decrypt, replay-check, persist
    P3->>UI: New verified event
    UI-->>O: Red potential-cyberattack popup
    E->>E: Return to baseline
    P2->>P2: Confirm recovery and re-arm
```

## Optional workstation detector

The `detector/` scripts provide a foreground detector for bench diagnostics.
Close OpenHantek first because only one process can own the scope.

```bash
cd detector
python3 -u button_cusum_monitor.py
```

This utility writes a local CSV unless `--log` is changed. CSV files are ignored
by Git.

For a longer generic experiment:

```bash
python3 detector/acs_scope_cusum.py \
  --baseline-seconds 120 \
  --log cusum_log.csv
```

## Operations

### Service status

Sensor nodes:

```bash
systemctl status layerone-node --no-pager
journalctl -u layerone-node -f
```

Pi3:

```bash
systemctl status layerone-server layerone-dashboard --no-pager
journalctl -u layerone-server -f
journalctl -u layerone-dashboard -f
```

### Restart and recalibrate Solar Panel 2

Restarting the service starts a new baseline calibration:

```bash
sudo systemctl restart layerone-node
journalctl -u layerone-node -f
```

Do this only while the measured target is in its known normal state.

### Foreground diagnosis

```bash
sudo systemctl stop layerone-node
python3 /home/russo/layerone/layerone.py node \
  --config /home/russo/layerone/node.json
```

Restore managed operation afterward:

```bash
sudo systemctl start layerone-node
```

### Dashboard API

```bash
curl -s http://127.0.0.1:8090/api/overview | python3 -m json.tool
```

The API returns node health, recent verified events, recent packet audit rows,
and receiver metadata.

## Troubleshooting

### Node is offline

1. Check power and network reachability.
2. Check `systemctl status layerone-node`.
3. Verify the LoRa adapter path and `dialout` membership.
4. Inspect `journalctl -u layerone-node -n 100`.
5. Confirm the transmitter and receiver use the same baud/radio settings.
6. Confirm Pi3 has enrolled the correct public keys.

### Hantek is unavailable

```bash
sigrok-cli --scan
sigrok-cli --driver hantek-6xxx --show
```

- close OpenHantek or any competing sigrok process;
- unplug/reconnect the scope;
- confirm `sigrok-firmware-fx2lafw` is installed;
- confirm the service user belongs to `plugdev`;
- inspect USB permissions with `lsusb` and `ls -l /dev/bus/usb/...`.

### LoRa serial port changed

```bash
ls -l /dev/ttyACM* /dev/ttyUSB*
python3 -m serial.tools.list_ports -v
```

Update `serial_port` in the node or server JSON and restart the service.

### Pi3 rejects packets

Check the packet audit and server journal. Common reasons are:

- node not enrolled or inactive;
- node public key changed after reinstallation;
- wrong Pi3 exchange public key in a node config;
- duplicate epoch/boot/counter tuple;
- damaged Base64 frame;
- invalid signature or AEAD authentication tag.

### False positives

- recalibrate with a genuinely quiet load;
- improve ACS grounding and USB noise control;
- increase `trigger_floor_mv`, `h_sigma`, or `votes_required` carefully;
- do not reduce thresholds merely to force a demonstration;
- compare normal operating modes before choosing production thresholds.

### No alert during the controlled test

- confirm Solar Panel 2 reports `state=ARMED`;
- confirm the keypad wiring and ESP32 serial phase labels;
- ensure CH1 measures the ACS output rather than the trigger pin;
- verify the workload shift exceeds `trigger_floor_mv` for at least three of five
  windows;
- inspect actual `mean`, `delta`, and CUSUM journal values before retuning.

## Data and key handling

Never commit or copy these runtime paths into the repository:

```text
/home/russo/layerone/keys/
/home/russo/layerone/data/
```

Recommended backup policy:

- back up Pi3's database and server private key to encrypted offline storage;
- back up each node's private keys separately;
- never consolidate all private keys on one machine;
- use file mode `0600` for private keys and JSON containing operational data;
- rotate and re-enroll a node if its private key may have been exposed.

The dashboard is a lab interface and does not add TLS or user authentication by
itself. Place it on an isolated management network or behind an authenticated
reverse proxy before broader deployment.

## Publish to GitHub

Review the tree before publishing:

```bash
find . -type f | sort
git status --short
```

Create a new repository history:

```bash
git init
git add .
git commit -m "Initial LayerOne MVP release"
git branch -M main
git remote add origin git@github.com:YOUR_ACCOUNT/LayerOne.git
git push -u origin main
```

Git records the commit date and file contents. It does not preserve ordinary
filesystem creation times. Do not add runtime keys, databases, logs, or build
artifacts to manufacture project history.

## Safety and scope

The included ESP32 workload is a controlled defensive demonstration. It has no
exploit, propagation, persistence, credential access, or external attack target.
Run it only on owned lab equipment with suitable current limiting, thermal
monitoring, and electrical isolation.
