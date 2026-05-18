# mesh-vision

Off-grid mesh networking with an AR heads-up display.
Simulates a Reticulum mesh on localhost, visualizes it in a WebGL HUD,
and is designed to run on real hardware (smart glasses + SBC) in Phase 2.

---

## Quick Start

```bash
# 1. Clone & enter
cd mesh-vision

# 2. Create venv & install deps
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 3. Launch everything
chmod +x scripts/start.sh
./scripts/start.sh
```

The script will:
- Activate the virtual environment
- Verify Reticulum configs for all 4 nodes
- Start the FastAPI backend on port 8420
- Open http://localhost:8420 in your browser

Press Ctrl+C to stop.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                        YOUR MACHINE                          │
│                                                              │
│   ┌──────────┐    TCP:4242    ┌────────────────┐             │
│   │  Node B  │◄──────────────►│   Transport    │             │
│   │ (glasses)│                │   Node         │             │
│   │ :37430   │                │   :37428       │             │
│   └──────────┘                │  enable_transport=True       │
│                               │  TCP Server :4242            │
│   ┌──────────┐    TCP:4242    │  AutoInterface (LAN)         │
│   │  Node C  │◄──────────────►│                │             │
│   │ (glasses)│                └───────┬────────┘             │
│   │ :37432   │                        │                      │
│   └──────────┘                        │ TCP:4242             │
│                                       │                      │
│   ┌───────────────────────────────────┴──────┐               │
│   │         Backend Aggregator Node          │               │
│   │         :37434                           │               │
│   │  ┌─────────────────────────────────┐     │               │
│   │  │  FastAPI Server (:8420)         │     │               │
│   │  │  ├── /api/mesh/status           │     │               │
│   │  │  ├── /api/mesh/send             │     │               │
│   │  │  ├── /api/mesh/peers            │     │               │
│   │  │  └── WebSocket /ws/mesh         │     │               │
│   │  └─────────────────────────────────┘     │               │
│   └──────────────────────────────────────────┘               │
│                         │                                    │
│                         │ HTTP / WebSocket                   │
│                         ▼                                    │
│   ┌──────────────────────────────────────────┐               │
│   │           Web HUD (Browser)              │               │
│   │  Three.js WebGL + AR overlay canvas      │               │
│   │  Real-time mesh topology graph           │               │
│   │  Message feed · Node status · Signal     │               │
│   └──────────────────────────────────────────┘               │
└──────────────────────────────────────────────────────────────┘
```

---

## Components

### Transport Node (`mesh_configs/transport/`)
Central relay with `enable_transport = True`. Runs a TCP server on
port 4242 that all other nodes connect to. Also has AutoInterface
enabled for LAN multicast discovery. This is the backbone of the
simulated mesh.

### Node B (`mesh_configs/node_b/`)
Simulated endpoint — represents a pair of AR glasses or a handheld
radio. Connects to the transport node via TCP. Has its own RNS
identity and can announce destinations, send/receive LXMessages.

### Node C (`mesh_configs/node_c/`)
Second simulated endpoint. Same as Node B but on different ports.
Demonstrates multi-hop routing through the transport node.

### Backend Aggregator (`mesh_configs/backend_node/`)
The FastAPI server's own Reticulum identity. Connects to the
transport node AND has AutoInterface enabled so it can see both
the simulated mesh and any real hardware on the LAN. Aggregates
mesh state and pushes updates to the web HUD via WebSocket.

### FastAPI Backend (`backend/server.py`)
REST + WebSocket API on port 8420. Manages RNS node lifecycle,
exposes mesh topology, routes messages, and serves the web HUD
as static files.

### Web HUD (`web/index.html`)
Browser-based heads-up display. Three.js renders the mesh topology
as a force-directed graph. Shows real-time node status, message
feed, signal strength indicators, and an AR-style overlay.

---

## Sending Messages Between Nodes

### Via the Web HUD
1. Open http://localhost:8420
2. Select a source node from the dropdown
3. Type a message and select a destination
4. Click Send — watch it route through the transport node

### Via the API
```bash
# Send from Node B to Node C
curl -X POST http://localhost:8420/api/mesh/send \
  -H "Content-Type: application/json" \
  -d '{
    "from_node": "node_b",
    "to_node": "node_c",
    "message": "Hello from the mesh!"
  }'

# Check mesh status
curl http://localhost:8420/api/mesh/status

# List discovered peers
curl http://localhost:8420/api/mesh/peers
```

### Via Python (direct RNS)
```python
import RNS

# Each node has its own config dir
reticulum = RNS.Reticulum("backend/mesh_configs/node_b")
identity = RNS.Identity()

# Create a destination and announce it
dest = RNS.Destination(identity, RNS.Destination.IN, RNS.Destination.SINGLE, "mesh", "node_b")
dest.announce()

print(f"Node B hash: {RNS.prettyhexrep(dest.hash)}")
```

---

## Port Map

| Node              | Instance Port | Control Port | TCP Listen |
|-------------------|--------------|--------------|------------|
| Transport         | 37428        | 37429        | 4242       |
| Node B            | 37430        | 37431        | —          |
| Node C            | 37432        | 37433        | —          |
| Backend Aggregator| 37434        | 37435        | —          |
| FastAPI Server    | —            | —            | 8420 (HTTP)|

---

## Phase 2 — Real AR Glasses Hardware

The simulation maps 1:1 to physical hardware. When ready:

1. **Replace TCP interfaces with RNodeInterface or SerialInterface**
   for actual LoRa radio links (RNode, LilyGO T-Beam, Heltec V3)

2. **Run Node B/C on Raspberry Pi Zero 2W** or similar SBC
   mounted on the glasses frame

3. **Feed camera/sensor data** into the backend for real AR overlays

4. **Use BLE or USB serial** between glasses display and the SBC

### Hardware Shopping List

| Item                          | Purpose                    | ~Price |
|-------------------------------|----------------------------|--------|
| XREAL Air 2 or Rokid Max 2   | AR display (USB-C glasses) | $300   |
| Raspberry Pi Zero 2W (×2)    | Edge compute, run RNS node | $30    |
| LilyGO T-Beam Supreme (×3)   | LoRa radio (RNode firmware)| $120   |
| 18650 battery + holder (×3)  | Power for T-Beams          | $30    |
| USB-C OTG cables             | Connect glasses to Pi      | $10    |
| MicroSD cards 32GB (×2)      | Pi OS + mesh-vision        | $15    |
| 3D printed frame mount       | Attach Pi + radio to frame | $5     |
| **Total**                     |                            |**~$510**|

### Recommended LoRa Settings (RNode)
- Frequency: 915 MHz (US) / 868 MHz (EU)
- Bandwidth: 125 kHz
- Spreading Factor: 8
- TX Power: 17 dBm
- Range: 1-5 km line of sight, 200-800m urban

---

## Project Structure

```
mesh-vision/
├── scripts/
│   └── start.sh              # Launch everything
├── backend/
│   ├── server.py              # FastAPI + RNS integration
│   └── mesh_configs/
│       ├── transport/config   # Transport relay node
│       ├── node_b/config      # Simulated endpoint B
│       ├── node_c/config      # Simulated endpoint C
│       └── backend_node/config# Backend aggregator
├── web/
│   └── index.html             # WebGL HUD
├── requirements.txt
└── README.md
```

---

## License

MIT — Build cool mesh stuff.
