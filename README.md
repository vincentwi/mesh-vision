# MeshVision — See the Mesh Through AR Glasses

Real-time mesh network visualization on smart glasses. WiFi spectrum analysis, encrypted mesh messaging, and human presence detection from WiFi signal fluctuations — all using real data, never simulated.

**Hardware:** MacBook (hub) + Raspberry Pi 3B+ (transport relay) + 2× RayNeo X3 Pro AR glasses + Mac mini (glasses host)

**Software:** Python/FastAPI backend, Reticulum mesh protocol, LXMF encrypted messaging, CoreWLAN WiFi sensing, Bleak BLE scanning, OpenCV optical flow

---

## What It Actually Does

When you put on the glasses, you see:

- **Radar** — every WiFi access point and BLE device around you as blips. Green = WiFi, blue = BLE, orange = mesh nodes. Distance from center = signal strength. Position is hash-based (not real physical direction — WiFi doesn't tell you which way a signal comes from).

- **Spectrum analyzer** — bar chart of WiFi channel utilization. Tall red bars = congested channels, short green = quiet. Same data a network engineer sees with a $500 spectrum analyzer tool.

- **Mesh topology** — real Reticulum connections between your devices. Lines appear when cryptographic handshakes complete. Lines disappear when nodes go offline. No fake nodes.

- **Vital signs** — breathing rate and heart rate estimated from WiFi signal fluctuations. Your body reflects/absorbs radio waves; when you breathe, the signal wobbles ~0.2 dBm at your breathing frequency. FFT extracts this.

- **Mesh chat** — encrypted LXMF messages that travel over Reticulum's transport layer, not HTTP. Works over TCP, serial, LoRa, or any byte stream.

---

## Architecture (Real, Not Simulated)

```
                          ┌─────────────────────────────────┐
                          │         RASPBERRY PI 3B+        │
                          │         10.0.10.82              │
                          │                                 │
                          │  rnsd (transport node)          │
                          │    TCP Server :4242             │
                          │    AutoInterface (LAN mcast)    │
                          │                                 │
                          │  MeshChat (Liam Cottle)         │
                          │    Web UI :8080                 │
                          │    LXMF identity: 1190da39...   │
                          │    LXMF dest:     22871c53...   │
                          └──────────┬──────────────────────┘
                                     │ TCP :4242
                                     │
┌────────────────────────────────────┴───────────────────────┐
│                      MACBOOK (Hub)                         │
│                      10.0.10.178 (Pi-Star WiFi)            │
│                      100.81.56.107 (Tailscale)             │
│                                                            │
│  rnsd (shared instance :37428)                             │
│    TCP Server :4243 ←── Mac mini connects here             │
│    TCP Client → Pi:4242                                    │
│    AutoInterface (LAN multicast)                           │
│                                                            │
│  MeshVision Backend (uvicorn :8420)                        │
│    ├── CoreWLAN: real WiFi scanning (10Hz RSSI + 15s full) │
│    ├── Bleak: BLE device scanning                          │
│    ├── OpenCV: camera optical flow for heading              │
│    ├── WiFi Sensing: breathing, heart rate, presence        │
│    ├── LXMF: encrypted mesh messaging                      │
│    ├── WebSocket: real-time push to HUD clients             │
│    └── REST API: 27 endpoints                              │
│                                                            │
│  Web HUD: http://localhost:8420 (index.html)               │
│  Glasses HUD: glasses.html (640×480, served via WebSocket) │
└────────────────────────────────────┬───────────────────────┘
                                     │ Tailscale VPN
                                     │
┌────────────────────────────────────┴───────────────────────┐
│                     MAC MINI                               │
│                     100.116.27.60 (Tailscale)              │
│                                                            │
│  rnsd → TCP to MacBook:4243 (via Tailscale)                │
│  ADB host for glasses (USB)                                │
│  SSH tunnel: mini:8420 → MacBook:8420                      │
│                                                            │
│  ┌─────────────┐     ┌─────────────┐                      │
│  │ Glasses-1   │ USB │ Glasses-2   │ USB                   │
│  │ A06B4A8FF.. │     │ A06B4A94C.. │                       │
│  │ RayNeo X3   │     │ RayNeo X3   │                       │
│  │ Pro         │     │ Pro         │                       │
│  │ MeshVision  │     │ MeshVision  │                       │
│  │ app (APK)   │     │ app (APK)   │                       │
│  └─────────────┘     └─────────────┘                       │
│                                                            │
│  Message path from glasses:                                │
│    curl localhost:8420 (on glasses)                         │
│    → ADB reverse tunnel (USB)                              │
│    → Mac mini :8420                                        │
│    → SSH tunnel (Tailscale)                                │
│    → MacBook backend :8420                                 │
│    → Reticulum LXMF (E2E encrypted)                       │
│    → TCP :4243 → Pi :4242                                  │
│    → Pi MeshChat                                           │
└────────────────────────────────────────────────────────────┘
```

---

## Quick Start (MacBook)

```bash
git clone https://github.com/vincentwi/mesh-vision.git
cd mesh-vision

# Create venv and install dependencies
python3 -m venv .venv
source .venv/bin/activate
pip install rns lxmf fastapi uvicorn bleak opencv-python-headless numpy pyobjc-framework-CoreWLAN pyobjc-framework-CoreBluetooth

# Start the shared Reticulum daemon (keeps running in background)
rnsd &

# Start the backend
cd backend
python3 -m uvicorn server:app --host 0.0.0.0 --port 8420 --log-level info

# Open http://localhost:8420 in your browser
```

### Requirements
- macOS (CoreWLAN for WiFi scanning — Linux would need iwlist/iw)
- Python 3.9+ (tested on 3.9.6)
- A WiFi connection (the scanner reads signal data from your connected AP)
- Camera (optional, for heading estimation via optical flow)

---

## Raspberry Pi Setup

See [pi-deploy/README.md](pi-deploy/README.md) for the full deployment guide.

```bash
# On the Pi (Raspberry Pi 3B+ or newer, Raspbian Bookworm)
pip3 install rns lxmf

# Install Reticulum daemon as a service
sudo cp pi-deploy/rnsd.service /etc/systemd/system/
sudo cp pi-deploy/reticulum-config ~/.reticulum/config
sudo systemctl enable --now rnsd

# Install MeshChat (Liam Cottle's web UI)
git clone https://github.com/liamcottle/reticulum-meshchat.git
cd reticulum-meshchat
pip3 install -r requirements.txt
sudo cp ../pi-deploy/meshchat.service /etc/systemd/system/
sudo systemctl enable --now meshchat

# CRITICAL: Run headless to avoid freezing
sudo systemctl set-default multi-user.target
```

### Why the Pi Freezes

The Raspberry Pi 3B+ has **921MB RAM**. The desktop GUI (labwc, panel, file manager, portal) consumes **225MB** just sitting idle. When Chromium opens MeshChat's Vue.js SPA, it eats another 300-500MB. The system starts swapping to the SD card at 1-2 MB/s (vs 1000+ MB/s for real RAM). It's not crashing — it's swap-thrashing so badly that input events never process.

**The fix:** Run headless. `sudo systemctl set-default multi-user.target` disables the desktop. Access MeshChat via `http://<pi-ip>:8080` from your Mac browser. After disabling the GUI, the Pi runs comfortably with 713MB available — rnsd (25MB) + meshchat (40MB) leave 650MB+ free.

---

## AR Glasses Setup (RayNeo X3 Pro)

The glasses run an Android WebView wrapper that loads `glasses.html` from app assets and connects to the backend via WebSocket.

### Building the APK

```bash
# Requires Android Studio JDK and SDK
export JAVA_HOME="/Applications/Android Studio.app/Contents/jbr/Contents/Home"
cd android-app
./gradlew assembleDebug
# APK at: app/build/outputs/apk/debug/app-debug.apk
```

### Mercury SDK

The app uses RayNeo's Mercury SDK (`MercuryAndroidSDK-v0.2.5-*.aar`) for:
- `BaseMirrorActivity` — binocular 640×480 WebView that renders on both lenses
- `TempleAction` — temple touch gestures (click, double-click, slide forward/back)
- The SDK AAR goes in `android-app/app/libs/`

**Known limitation:** `am start` via ADB does not work on Mercury OS — returns "Activity class does not exist" even though it's registered. You must launch from the glasses app drawer manually.

### Deploying to Glasses via Mac mini

```bash
# Both glasses connected via USB to Mac mini
ADB=/opt/homebrew/bin/adb

# Install on both
$ADB -s A06B4A8FF4A1633 install -r app-debug.apk
$ADB -s A06B4A94CC51663 install -r app-debug.apk

# Set up ADB reverse tunnel so glasses reach the MacBook backend
$ADB -s A06B4A8FF4A1633 reverse tcp:8420 tcp:8420
$ADB -s A06B4A94CC51663 reverse tcp:8420 tcp:8420

# SSH tunnel on Mac mini: forward mini:8420 → MacBook:8420
ssh -f -N -L 0.0.0.0:8420:localhost:8420 vinceroy@<macbook-tailscale-ip>
```

### Backend URL Resolution (glasses.html)

When loaded from `file:///android_asset/`, `location.host` is empty. The glasses.html uses a fallback chain:
1. `window.MESH_BACKEND_URL` (injected by Android Activity via JavascriptInterface)
2. `localStorage.getItem('mesh_backend_url')`
3. `location.host` (works when served by the backend directly)

The Android Activity injects the URL after page load and calls `window.reconnectWS()` to re-establish the WebSocket with the correct host.

---

## WiFi Sensing — How It Works

### RSSI-Based Sensing (Current)

The MacBook's CoreWLAN chip reports signal strength (RSSI) 10 times per second. This stream feeds three detectors:

**Breathing detection:** Your chest expanding/contracting modulates the WiFi signal by ~0.2 dBm. FFT decomposes the RSSI stream and looks for peaks in the 0.15–0.5 Hz range (9–30 breaths per minute). Confidence indicates how prominent the peak is above noise.

**Heart rate detection:** Same principle but looking at 0.8–2.0 Hz (48–120 BPM). Much harder — the signal modulation from heartbeat is ~0.05 dBm, buried in noise. Confidence is typically 0.1–0.2 with RSSI alone. Research papers only achieve reliable results with CSI hardware and a stationary subject within 3 feet.

**Presence detection:** Compares current 30-second RSSI variance against a baseline captured during the first 10 seconds. A person moving between the laptop and router causes variance spikes. The baseline auto-resets when the connected SSID changes (new WiFi = new radio environment).

**Motion detection:** Instantaneous RSSI variance over 1-second windows. Walking past the laptop causes the signal to drop sharply then recover — variance spikes to 0.3–0.8 from a baseline of ~0.1.

### CSI-Based Sensing (Next Phase — ESP32 Hardware)

RSSI gives 1 number per reading. CSI (Channel State Information) gives **56–234 subcarrier measurements** including amplitude AND phase. It's like going from 1 microphone to 234 microphones.

**Recommended hardware:**
| Board | Chip | WiFi | Subcarriers (20MHz) | Best For | Price |
|-------|------|------|---------------------|----------|-------|
| Seeed XIAO ESP32-C6 | ESP32-C6 | WiFi 6 (802.11ax) | 234 | CSI sensing (4.5× more data) | ~$5 |
| YEJMKJ ESP32-S3-N16R8 | ESP32-S3 | WiFi 4 (802.11n) | 52 | On-device TinyML inference | ~$12 |

The existing `Esp32CsiReceiver` class in server.py listens on UDP:5005 for CSI data streams from ESP32 devices.

### Key Limitation: macOS Hides BSSIDs

Apple's privacy policy prevents CoreWLAN from exposing router MAC addresses (BSSIDs) to unprivileged apps. The EnvironmentMapper uses SSID+channel as a composite key instead. This means two APs with the same SSID on the same channel appear as one. Not fixable without an entitlement from Apple.

---

## Mesh Networking — Reticulum + LXMF

### What Reticulum Is

Reticulum is a cryptography-first network stack that works over any medium — TCP, UDP, serial, LoRa radio, packet radio, or any byte stream. Every node gets an address derived from its cryptographic public key. Messages are encrypted end-to-end; relay nodes can't read them.

### What LXMF Is

Lightweight Extensible Message Format — an email-like protocol on Reticulum. Each device has an LXMF "delivery" destination. Messages are routed through transport nodes (relays) to reach the destination.

**Critical distinction:** A node's LXMF *destination* hash is NOT the same as its *identity* hash. The destination hash is derived from `identity_hash + "lxmf" + "delivery"`. We learned this the hard way — the Pi MeshChat's identity hash is `1190da39...` but its LXMF destination (what you actually address messages to) is `22871c53...`.

### Transport Topology

```
MacBook rnsd (shared instance, :37428)
  ├── AutoInterface (LAN multicast — discovers nearby nodes automatically)
  ├── TCP Client → Pi:4242 (direct mesh link)
  └── TCP Server :4243 ← Mac mini connects here

Pi rnsd (transport node, enables forwarding)
  ├── AutoInterface (LAN multicast)
  └── TCP Server :4242 ← MacBook connects here

Mac mini rnsd
  ├── AutoInterface
  └── TCP Client → MacBook:4243 (via Tailscale VPN)
```

### Sending a Mesh Message (Proven Working)

```bash
# From any device that can reach the backend:
curl -X POST http://localhost:8420/api/send-message \
  -H "Content-Type: application/json" \
  -d '{"content": "Hello mesh!", "to_hash": "22871c5306bf067746f09cc4ea819dde"}'

# Or run the proof script:
python3 backend/mesh_proof.py
```

---

## API Reference

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/status` | GET | Backend health, WiFi/BLE counts, mesh readiness |
| `/api/wifi` | GET | All visible WiFi networks with RSSI, channel, security |
| `/api/ble` | GET | All visible BLE devices with RSSI |
| `/api/topology` | GET | Mesh nodes and links (real Reticulum announces only) |
| `/api/vital-signs` | GET | Breathing, heart rate, motion, presence |
| `/api/wifi-sensing` | GET | Detailed RSSI analysis with sparkline |
| `/api/presence` | GET | Person count, activity, presence blobs |
| `/api/environment` | GET | AP landscape: 515+ tracked APs, channel map, anomalies |
| `/api/wifi-profile` | GET | Current SSID and profile (work/home) |
| `/api/wifi-heatmap` | GET | Signal strength by direction sector |
| `/api/ble-density` | GET | BLE device density by direction sector |
| `/api/camera` | GET | Optical flow heading estimate |
| `/api/send-message` | POST | Send LXMF message over Reticulum mesh |
| `/api/messages` | GET | Message history |
| `/api/mesh-chat/status` | GET | Pi MeshChat reachability |
| `/api/mesh-chat/messages` | GET | Messages from Pi MeshChat |
| `/api/voice-chat` | POST | Voice-to-mesh: upload audio → Groq Whisper STT → LXMF |
| `/api/sensing/reset-baseline` | POST | Force-reset presence detection baseline |
| `/api/peers` | GET | Discovered Reticulum peers |
| `/api/map/start` | POST | Start WiFi mapping walk-around |
| `/api/map/mark` | POST | Mark a position during mapping |
| `/api/map/stop` | POST | Stop mapping and compute heatmap |
| `/api/map/data` | GET | Mapping results |
| `/api/map/depth-scan` | POST | Upload iPhone depth scan for floor plan |
| `/ws/mesh` | WebSocket | Real-time push of all data (1 fps) |

---

## Failures, Dead Ends, and Lessons Learned

This section documents every wrong turn so you don't repeat them.

### 1. Simulated Mesh Nodes Were Dishonest

**What we did:** Initially spawned fake rnsd subprocesses (Transport-A, Node-B, Node-C) and an `auto_messenger_loop` that generated fake messages. The HUD showed a lively mesh with 4 nodes and flowing messages.

**Why it failed:** It looked great in demos but was a lie. When the user asked "is the Pi actually connected?" the answer was no — the topology was pre-seeded. The fake nodes masked real connectivity issues.

**The fix:** Stripped ALL simulated nodes, fake messages, and pre-seeded peers. Now the topology shows ONLY nodes discovered via real Reticulum announces. If the Pi is offline, the topology shows one lonely node. Honest.

**Lesson:** Never fake data in a sensing application. Users will ask "is this real?" and you need to answer yes.

### 2. CoreWLAN Threading Nightmare

**What we did:** Created a `FastRssiMonitor` class that spawned its own daemon thread to read RSSI at 10 Hz from CoreWLAN.

**Why it failed:** `CWWiFiClient.rssiValue()` works in the thread that originally obtained the CWWiFiClient reference, but fails silently when called from a different thread under uvicorn's event loop. The RSSI values came back as 0 or threw Objective-C exceptions.

**The fix:** Moved the 10 Hz RSSI reads INTO the existing `wifi_scan_loop` thread (which already has a working CoreWLAN context). Between full scans (~15s each), the loop does 40 rapid reads at 100ms intervals, feeding them to FastRssiMonitor via `push_reading()`.

**Lesson:** PyObjC + CoreWLAN are thread-sensitive. Create the CWWiFiClient once in a thread and do ALL CoreWLAN work in that same thread.

### 3. BSSID Privacy on macOS

**What we did:** Used BSSID (router MAC address) as the unique key for tracking APs in EnvironmentMapper.

**Why it failed:** macOS returns empty string for `CWNetwork.bssid()` unless the app has a specific Apple entitlement. All 134 APs showed up with bssid="" — so the mapper saw them as one AP.

**The fix:** Composite key: `SSID@chN` (e.g., `Pi-Star@ch11`). Not perfect — two APs with the same name on the same channel collapse into one — but it works for 95% of cases.

**Lesson:** Apple's privacy model hides BSSIDs, Location, and other identifiers. Check what data CoreWLAN actually returns before building data structures around it.

### 4. Room Change Detection Oscillation

**What we did (attempt 1):** Compared current scan's AP set against ALL tracked APs using Jaccard similarity. Threshold: 0.80.

**Why it failed:** Each scan sees ~25 APs; the tracker accumulates ~500 over time. Jaccard(25, 500) ≈ 0.05 — perpetual "room change" even when sitting still.

**What we did (attempt 2):** Changed to 90-second rolling window (~90 recent APs) with threshold 0.30.

**Why it failed:** Jaccard(25, 90) ≈ 0.28 — still oscillating every scan. "Room change detected" / "Room stabilized" every 15 seconds.

**What we did (attempt 3, current):** Compare consecutive scans (scan N vs scan N-1). Two back-to-back scans from the same room overlap ~70-90%. Require 3 consecutive scans below 0.40 to confirm a room change.

**Lesson:** WiFi scans are inherently noisy — each scan is a random sample of nearby APs. Never compare a single scan against an accumulated set. Compare peer scans and require sustained change.

### 5. Presence Baseline Stuck From Office

**What we did:** Set the presence baseline during the first 10 seconds of readings — once, at startup.

**Why it failed:** If you start the backend at home, the baseline captures HOME's radio environment. Then it never updates. When you go to the office, presence detection is calibrated for the wrong environment. Or worse — if you're already sitting at your desk when it starts, your breathing gets baked into the "empty room" baseline.

**The fix:** Added `reset_baseline()` method + `POST /api/sensing/reset-baseline` endpoint + auto-reset on SSID change (new WiFi network = new radio environment) + auto-reset on confirmed room change.

**Lesson:** Any baseline-comparison system needs a re-baseline mechanism. Environmental sensing baselines go stale when the environment changes.

### 6. LXMF Destination Hash ≠ Identity Hash

**What we did:** Hardcoded `PI_LXMF_HASH = "1190da39..."` (the Pi's identity hash) and used it for path lookups and message routing.

**Why it failed:** Reticulum's `RNS.Transport.has_path()` takes a DESTINATION hash, not an identity hash. The LXMF destination hash is derived from `identity + "lxmf" + "delivery"` — a completely different value. Path lookups returned false; messages couldn't route.

**The fix:** Used the actual LXMF destination hash `22871c53...` (discovered from announces). Also fixed `send_lxmf_message()` which was missing `RNS.Destination.SINGLE` — passing `"lxmf"` where RNS expected a direction constant.

**Lesson:** Reticulum has a clear distinction between identity (who you are) and destination (what service you're running). Read the Reticulum docs on destination types carefully.

### 7. MeshChat Auto-Announce Was Disabled

**What we did:** Expected Pi MeshChat to automatically announce its LXMF destination after boot.

**Why it failed:** MeshChat's `auto_announce_enabled` config defaults to false (or requires web UI interaction to enable). The Pi would run for hours without ever announcing, so the MacBook never discovered it.

**The fix:** Hit `curl http://pi-ip:8080/api/v1/announce` after MeshChat starts. Could be automated in a systemd ExecStartPost or a cron job.

**Lesson:** Don't assume services will announce themselves. Check if auto-announce is enabled, and have a fallback trigger.

### 8. ADB Can't Launch Mercury OS Apps

**What we did:** Tried `adb shell am start -n com.meshvision/.MeshVisionActivity` to launch the app remotely.

**Why it failed:** Mercury OS (RayNeo's custom Android) blocks `am start` for third-party activities — returns "Activity class does not exist" even though it's correctly declared in AndroidManifest.xml with MAIN/LAUNCHER intent filters.

**Workaround:** Must launch from the glasses' app drawer manually. ADB can install, uninstall, and interact with the app once running, but can't cold-start it.

### 9. Glasses WiFi Was Disconnected

**What we did:** Assumed the glasses would auto-connect to WiFi and reach the backend.

**Why it failed:** The glasses had "Pi-Star" WiFi saved but couldn't connect (possibly out of range, or the Pi was down). With no WiFi, the glasses had no network at all.

**The fix:** ADB reverse tunnel over USB. `adb reverse tcp:8420 tcp:8420` maps the glasses' `localhost:8420` to the host machine's port 8420. Combined with an SSH tunnel on the Mac mini to the MacBook, this creates a USB → tunnel → VPN → backend chain that works without any WiFi on the glasses.

**Lesson:** Don't depend on WiFi for devices that are physically USB-connected. ADB reverse tunnels are more reliable.

---

## File Reference

| File | Lines | Description |
|------|-------|-------------|
| `backend/server.py` | 2227 | FastAPI backend: mesh, WiFi, BLE, camera, sensing, 27 API endpoints |
| `backend/wifi_sensing.py` | 1177 | FastRssiMonitor (10Hz), EnvironmentMapper (AP tracking), HumanPresenceEstimator |
| `backend/mesh_proof.py` | 85 | Standalone script proving Mac→Pi mesh communication |
| `backend/meshvision_identity` | - | Persistent LXMF identity file (hash: 4a30264a...) |
| `web/index.html` | 2421 | Full Mac HUD: radar, spectrum, topology, vital signs, mesh chat |
| `web/glasses.html` | 839 | Simplified glasses HUD: radar, messages, vital signs, voice |
| `web/office-map.html` | 1021 | WiFi office mapping with walk-around trilateration |
| `android-app/...MeshVisionActivity.kt` | 243 | Android WebView wrapper for RayNeo X3 Pro (Mercury SDK) |
| `android-app/.../glasses.html` | 839 | Copy of glasses HUD bundled in APK assets |
| `pi-deploy/rnsd.service` | - | systemd unit for Reticulum daemon |
| `pi-deploy/meshchat.service` | - | systemd unit for Liam Cottle's MeshChat |
| `pi-deploy/reticulum-config` | - | Pi's Reticulum config (transport node, TCP:4242) |

---

## Network Reference

| Device | IP (Pi-Star WiFi) | IP (Tailscale) | Reticulum Port | Service Port |
|--------|-------------------|----------------|----------------|-------------|
| MacBook | 10.0.10.178 | 100.81.56.107 | TCP:4243 (server) | 8420 (backend) |
| Raspberry Pi | 10.0.10.82 | — | TCP:4242 (server) | 8080 (MeshChat) |
| Mac mini | — | 100.116.27.60 | TCP→MacBook:4243 | ADB host |
| Glasses-1 | — | — | — | via ADB reverse |
| Glasses-2 | — | — | — | via ADB reverse |

### Reticulum Identity Map

| Device | Identity Hash | LXMF Destination Hash | Display Name |
|--------|--------------|----------------------|-------------|
| MacBook | 44c998b6... | 4a30264a9b9faa25... | MeshVision-Mac |
| Pi MeshChat | 1190da39b618577f... | 22871c5306bf0677... | 92c40e416e6f6e79 |
| Pi Transport | — | 03b7237d5e1c44df... | (transport relay) |

---

## WiFi Profiles

The backend detects the connected SSID and maps it to a profile:

| SSID | Profile | Behavior |
|------|---------|----------|
| Pi-Star | `work` | Office environment — stronger signals, more APs |
| (anything else) | `home` | Home environment — weaker signals, fewer APs |

When the SSID changes, the presence baseline auto-resets because the radio environment is completely different.

---

## What's Next

1. **ESP32 CSI hardware** — 2× Seeed XIAO ESP32-C6 ($5 each) for WiFi 6 CSI sensing (234 subcarriers vs RSSI's 1 number). The `Esp32CsiReceiver` class in server.py already listens on UDP:5005.

2. **LoRa radio** — Replace TCP transport with LoRa (RNode firmware on ESP32 + SX1276). Same Reticulum protocol, no WiFi needed. Range: 1-5km line of sight.

3. **Office WiFi mapping** — The `office-map.html` tool lets you walk around recording WiFi fingerprints to build a signal heatmap of your space.

4. **iPhone depth scan** — Upload LiDAR depth scans from iPhone to extract floor plans for the office map.

5. **Mac mini launchd** — The Mac mini's rnsd runs via nohup (not persistent across reboots). Needs a launchd plist.

---

## Credits & References

- **Reticulum** — [reticulum.network](https://reticulum.network/) by Mark Qvist. The mesh protocol that makes this work.
- **LXMF** — [github.com/markqvist/LXMF](https://github.com/markqvist/LXMF). Encrypted messaging on Reticulum.
- **MeshChat** — [github.com/liamcottle/reticulum-meshchat](https://github.com/liamcottle/reticulum-meshchat) by Liam Cottle. Web UI for Reticulum messaging.
- **NomadNet** — [github.com/markqvist/nomadnet](https://github.com/markqvist/nomadnet). Terminal UI for Reticulum.
- **ESP-CSI** — [Espressif CSI docs](https://docs.espressif.com/projects/esp-techpedia/en/latest/esp-friends/solution-introduction/esp-csi/esp-csi-solution.html). WiFi sensing on ESP32.
- **ESP32-CSI-Tool** — [stevenmhernandez.github.io/ESP32-CSI-Tool](https://stevenmhernandez.github.io/ESP32-CSI-Tool/). Research toolkit for WiFi sensing.
- **Mercury SDK** — RayNeo's Android SDK for X3 Pro AR glasses (binocular rendering, temple gestures).
- **Related projects:** [PokeMesh](https://github.com/IdreesInc/PokeMesh), [BitChat](https://github.com/permissionlesstech/bitchat), [BeatSync](https://github.com/freeman-jiang/beatsync), [MeshCore](https://github.com/zjs81/meshcore-open).

---

## License

MIT — Build cool mesh stuff.
