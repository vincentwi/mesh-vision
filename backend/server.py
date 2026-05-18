#!/usr/bin/env python3
"""
MeshVision FastAPI Backend — Real Reticulum Mesh Edition
=========================================================
Connects to the Mac's SHARED Reticulum instance (rnsd already running),
creates an LXMF identity for real mesh messaging, discovers peers via
announces, and builds topology from actual RNS path tables.

All WiFi sensing, BLE scanning, camera orientation, directional signal
enrichment, and HUD features are preserved.

NO simulated nodes. NO fake messages. NO subprocess rnsd spawning.
Every mesh node, link, and message is REAL.

Python 3.9 compatible. macOS-focused.
"""

import asyncio
import base64
import hashlib
import json
import logging
import math
import os
import random
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union

import numpy as np
import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

# WiFi sensing module (real RSSI monitor, environment mapper, presence estimator)
try:
    from wifi_sensing import (
        FastRssiMonitor, EnvironmentMapper, HumanPresenceEstimator,
        build_sensing_payload,
    )
    HAS_SENSING = True
except ImportError as exc:
    HAS_SENSING = False
    logging.getLogger("meshvision").warning("wifi_sensing module not available: %s", exc)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("meshvision")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BACKEND_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BACKEND_DIR.parent
WEB_DIR = PROJECT_DIR / "web"
GLASSES_DIR = PROJECT_DIR.parent  # ~/Desktop/APP/Glasses

# ---------------------------------------------------------------------------
# Groq STT Configuration
# ---------------------------------------------------------------------------
GROQ_STT_URL = "https://api.groq.com/openai/v1/audio/transcriptions"


def _load_groq_key() -> str:
    """Load Groq API key from env or local.properties fallback."""
    key = os.environ.get("GROQ_API_KEY", "")
    if key:
        return key
    prop_path = GLASSES_DIR / "listening-cone" / "local.properties"
    if prop_path.exists():
        for line in prop_path.read_text().splitlines():
            if line.startswith("GROQ_API_KEY="):
                return line.split("=", 1)[1].strip()
    return ""


GROQ_API_KEY = _load_groq_key()

# ---------------------------------------------------------------------------
# Known Pi MeshChat identity (pre-configured for topology display)
# ---------------------------------------------------------------------------
PI_LXMF_HASH = "1190da39b618577fbe35527d60dcc03f"
PI_TRANSPORT_HASH = "03b7237d5e1c44dfcbcb517edc90cefc"

# ---------------------------------------------------------------------------
# Utility: deterministic hash for azimuth assignment
# ---------------------------------------------------------------------------
def azimuth_hash(s: str) -> int:
    """Deterministic hash of a string to 0-359 degrees."""
    h = 0
    for i, c in enumerate(s):
        h += ord(c) * (31 ** i)
    return h % 360


def angular_diff(a: float, b: float) -> float:
    """Signed angular difference in degrees, result in [-180, 180]."""
    d = (a - b) % 360
    if d > 180:
        d -= 360
    return d


def direction_boost(camera_yaw: float, signal_azimuth: float) -> float:
    """Cosine-based boost: 1.0 when facing the signal, 0.0 when facing away."""
    diff = angular_diff(camera_yaw, signal_azimuth)
    boost = math.cos(math.radians(diff))
    return max(0.0, min(1.0, boost))


# ---------------------------------------------------------------------------
# Global shared state (thread-safe via GIL for simple reads/writes)
# ---------------------------------------------------------------------------
wifi_results = []       # type: List[Dict[str, Any]]
ble_results = []        # type: List[Dict[str, Any]]
mesh_nodes = []         # type: List[Dict[str, Any]]
mesh_links = []         # type: List[Dict[str, Any]]
mesh_messages = []      # type: List[Dict[str, Any]]
camera_state = {
    "active": False,
    "yaw_estimate": 0.0,
    "pitch_estimate": 0.0,
    "heading": 0.0,
    "angular_velocity": 0.0,
    "movement_magnitude": 0.0,
}  # type: Dict[str, Any]

# Cumulative camera heading
_camera_cumulative_yaw = 0.0   # type: float
_camera_heading_lock = threading.Lock()

# Lock for mesh_messages list
messages_lock = threading.Lock()

# Active WebSocket connections
ws_clients = set()  # type: Set[WebSocket]

# ---------------------------------------------------------------------------
# Link quality history storage
# ---------------------------------------------------------------------------
link_quality_history = {}  # type: Dict[str, List[Tuple[float, float]]]
link_quality_lock = threading.Lock()
LINK_HISTORY_MAX = 60


def _update_link_quality(link_key: str, quality: float) -> List[List[float]]:
    """Append a quality sample and return the last 30 entries."""
    now = time.time()
    with link_quality_lock:
        if link_key not in link_quality_history:
            link_quality_history[link_key] = []
        hist = link_quality_history[link_key]
        hist.append((now, quality))
        if len(hist) > LINK_HISTORY_MAX:
            del hist[:-LINK_HISTORY_MAX]
        return [[round(t, 2), round(q, 4)] for t, q in hist[-30:]]


# ---------------------------------------------------------------------------
# WiFi-sensing globals (real engines)
# ---------------------------------------------------------------------------
fast_rssi = None       # type: Optional[FastRssiMonitor]
env_mapper = None      # type: Optional[EnvironmentMapper]
presence_est = None    # type: Optional[HumanPresenceEstimator]

# ---------------------------------------------------------------------------
# Multi-user shared state
# ---------------------------------------------------------------------------
shared_users = {}  # type: Dict[str, Dict[str, Any]]
shared_users_lock = threading.Lock()
SHARED_USER_TTL = 60


def register_user(name: str, lat: float, lon: float, heading: float) -> None:
    with shared_users_lock:
        shared_users[name] = {
            "name": name, "lat": lat, "lon": lon,
            "heading": heading, "last_seen": time.time(),
        }


def get_active_users() -> List[Dict[str, Any]]:
    now = time.time()
    with shared_users_lock:
        expired = [k for k, v in shared_users.items() if now - v["last_seen"] > SHARED_USER_TTL]
        for k in expired:
            del shared_users[k]
        return list(shared_users.values())


# Camera heading reset command flag
_camera_reset_heading = False


# ===================================================================
#  REAL RETICULUM / LXMF MESH INTEGRATION
# ===================================================================
try:
    import RNS
    import LXMF
    HAS_RNS = True
    log.info("[mesh] Reticulum (RNS %s) and LXMF (%s) loaded.",
             getattr(RNS, '__version__', '?'), getattr(LXMF, '__version__', '?'))
except ImportError as exc:
    HAS_RNS = False
    RNS = None   # type: ignore
    LXMF = None  # type: ignore
    log.warning("[mesh] RNS/LXMF not available — real mesh disabled: %s", exc)

# Globals for the real mesh
reticulum_instance = None   # type: Any  # RNS.Reticulum
lxmf_router = None          # type: Any  # LXMF.LXMRouter
lxmf_destination = None     # type: Any  # RNS.Destination (our LXMF delivery dest)
mesh_identity = None         # type: Any  # RNS.Identity
mesh_ready = False

# Discovered LXMF peers from announces
# Key: hex destination hash, Value: {name, identity, last_seen, app_data, hops}
discovered_peers = {}  # type: Dict[str, Dict[str, Any]]
discovered_peers_lock = threading.Lock()


class LXMFAnnounceHandler:
    """Handles incoming LXMF delivery announces from the Reticulum network.
    Registered with RNS.Transport to discover mesh peers (Pi MeshChat,
    NomadNet, Sideband, other LXMF nodes)."""

    def __init__(self):
        self.aspect_filter = "lxmf.delivery"

    def received_announce(self, destination_hash, announced_identity, app_data):
        # type: (bytes, Any, Optional[bytes]) -> None
        try:
            hex_hash = destination_hash.hex()
            display_name = ""
            if app_data:
                try:
                    display_name = app_data.decode("utf-8")
                except Exception:
                    display_name = app_data.hex()[:16]

            with discovered_peers_lock:
                is_new = hex_hash not in discovered_peers
                discovered_peers[hex_hash] = {
                    "name": display_name or hex_hash[:12],
                    "identity": announced_identity,
                    "last_seen": time.time(),
                    "app_data": app_data,
                    "hops": -1,  # will be filled by topology refresh
                }

            verb = "Discovered NEW" if is_new else "Updated"
            log.info("[mesh] %s LXMF peer: %s (%s)", verb, display_name or "?", hex_hash[:16])
        except Exception:
            log.exception("[mesh] Error in announce handler")


def on_lxmf_delivery(message):
    """Callback for incoming LXMF messages delivered to our identity."""
    try:
        content = ""
        if message.content:
            if isinstance(message.content, bytes):
                content = message.content.decode("utf-8", errors="replace")
            else:
                content = str(message.content)

        source_hash = message.source_hash.hex() if message.source_hash else "unknown"
        msg_hash = message.hash.hex() if message.hash else ""

        # Look up source display name
        with discovered_peers_lock:
            peer = discovered_peers.get(source_hash, {})
        from_name = peer.get("name", source_hash[:12])

        # Determine hops
        hops = 0
        try:
            if HAS_RNS and message.source_hash:
                hops = RNS.Transport.hops_to(message.source_hash)
        except Exception:
            hops = -1

        entry = {
            "from_name": from_name,
            "from_hash": source_hash,
            "to_name": "MeshVision-Mac",
            "content": content,
            "timestamp": message.timestamp if hasattr(message, 'timestamp') and message.timestamp else time.time(),
            "hops": hops,
            "delivered": True,
            "source": "lxmf_mesh",
            "lxmf_hash": msg_hash,
        }
        with messages_lock:
            mesh_messages.append(entry)
            if len(mesh_messages) > 200:
                del mesh_messages[:-200]

        log.info("[mesh] LXMF received: %s -> us: %s", from_name, content[:80])
    except Exception:
        log.exception("[mesh] Error processing LXMF delivery")


def init_real_mesh() -> bool:
    """Initialize connection to the shared Reticulum instance and set up LXMF.
    Returns True on success."""
    global reticulum_instance, lxmf_router, lxmf_destination, mesh_identity, mesh_ready

    if not HAS_RNS:
        log.warning("[mesh] RNS/LXMF not available, skipping mesh init")
        return False

    try:
        # Connect to the shared Reticulum instance (rnsd must be running)
        log.info("[mesh] Connecting to shared Reticulum instance ...")
        reticulum_instance = RNS.Reticulum()
        log.info("[mesh] Reticulum connected (shared instance)")

        # Create or load persistent identity
        id_path = BACKEND_DIR / "meshvision_identity"
        if id_path.exists():
            mesh_identity = RNS.Identity.from_file(str(id_path))
            log.info("[mesh] Loaded existing identity from %s", id_path)
        else:
            mesh_identity = RNS.Identity()
            mesh_identity.to_file(str(id_path))
            log.info("[mesh] Created new identity, saved to %s", id_path)

        # LXMF storage directory
        lxmf_storage = BACKEND_DIR / "lxmf_storage"
        lxmf_storage.mkdir(parents=True, exist_ok=True)

        # Create LXMF router
        lxmf_router = LXMF.LXMRouter(
            identity=mesh_identity,
            storagepath=str(lxmf_storage),
        )
        log.info("[mesh] LXMF router created (storage: %s)", lxmf_storage)

        # Register delivery callback for incoming messages
        lxmf_router.register_delivery_callback(on_lxmf_delivery)

        # Register our delivery identity (makes us addressable on the mesh)
        lxmf_destination = lxmf_router.register_delivery_identity(
            mesh_identity,
            display_name="MeshVision-Mac",
        )
        our_hash = lxmf_destination.hash.hex()
        log.info("[mesh] LXMF destination registered: %s (MeshVision-Mac)", our_hash)

        # Register announce handler to discover other LXMF peers
        RNS.Transport.register_announce_handler(LXMFAnnounceHandler())
        log.info("[mesh] LXMF announce handler registered")

        # Pre-seed the Pi as a known peer (we'll update when its announce arrives)
        with discovered_peers_lock:
            discovered_peers[PI_LXMF_HASH] = {
                "name": "Pi-MeshChat",
                "identity": None,
                "last_seen": 0,
                "app_data": None,
                "hops": -1,
            }
        log.info("[mesh] Pre-seeded Pi MeshChat peer: %s", PI_LXMF_HASH[:16])

        # Send initial announce so peers can discover us
        try:
            lxmf_destination.announce()
            log.info("[mesh] Initial LXMF announce sent")
        except Exception:
            log.warning("[mesh] Initial announce failed (will retry)")

        mesh_ready = True
        log.info("[mesh] *** Real mesh initialization complete ***")
        return True

    except Exception:
        log.exception("[mesh] Failed to initialize real mesh")
        mesh_ready = False
        return False


def send_lxmf_message(content: str, to_hash: Optional[str] = None) -> Dict[str, Any]:
    """Send a real LXMF message over the Reticulum mesh.

    Args:
        content: Message text
        to_hash: Hex destination hash of recipient. If None, records locally only.

    Returns:
        Dict with status info.
    """
    if not mesh_ready or lxmf_router is None or lxmf_destination is None:
        return {"error": "Mesh not ready"}

    our_hash = lxmf_destination.hash.hex()

    # Record locally regardless
    entry = {
        "from_name": "MeshVision-Mac",
        "from_hash": our_hash,
        "to_name": "",
        "to_hash": to_hash or "",
        "content": content,
        "timestamp": time.time(),
        "hops": 0,
        "delivered": False,
        "source": "local_send",
    }

    if not to_hash:
        # No destination — local only
        entry["to_name"] = "Mesh (local)"
        with messages_lock:
            mesh_messages.append(entry)
            if len(mesh_messages) > 200:
                del mesh_messages[:-200]
        return {"status": "local_only", "content": content[:100]}

    try:
        dest_hash_bytes = bytes.fromhex(to_hash)

        # Look up peer name
        with discovered_peers_lock:
            peer = discovered_peers.get(to_hash, {})
        entry["to_name"] = peer.get("name", to_hash[:12])

        # Recall the destination identity (populated from announces)
        dest_identity = RNS.Identity.recall(dest_hash_bytes)
        if dest_identity is None:
            # Try requesting the path and wait briefly
            log.info("[mesh] Identity not recalled for %s, requesting path ...", to_hash[:16])
            RNS.Transport.request_path(dest_hash_bytes)
            time.sleep(3)
            dest_identity = RNS.Identity.recall(dest_hash_bytes)

        if dest_identity is None:
            entry["delivered"] = False
            with messages_lock:
                mesh_messages.append(entry)
                if len(mesh_messages) > 200:
                    del mesh_messages[:-200]
            return {"error": "Could not recall identity for {}. Peer may not have announced yet.".format(to_hash[:16])}

        # Build LXMF destination for recipient
        dest = RNS.Destination(
            dest_identity,
            RNS.Destination.OUT,
            "lxmf", "delivery",
        )

        # Create and send LXMF message
        lxmsg = LXMF.LXMessage(
            dest,
            lxmf_destination,
            content.encode("utf-8"),
            title="",
            desired_method=LXMF.LXMessage.DIRECT,
        )

        # Register delivery/failure callbacks
        def _on_delivered(msg):
            log.info("[mesh] LXMF message delivered to %s", to_hash[:16])
            # Update the entry in mesh_messages
            with messages_lock:
                for m in reversed(mesh_messages):
                    if m.get("content") == content and m.get("to_hash") == to_hash:
                        m["delivered"] = True
                        break

        def _on_failed(msg):
            log.warning("[mesh] LXMF message FAILED to %s", to_hash[:16])

        lxmsg.register_delivery_callback(_on_delivered)
        lxmsg.register_failed_callback(_on_failed)

        lxmf_router.handle_outbound(lxmsg)
        entry["lxmf_hash"] = lxmsg.hash.hex() if lxmsg.hash else ""
        log.info("[mesh] LXMF message queued: us -> %s: %s", to_hash[:16], content[:60])

        with messages_lock:
            mesh_messages.append(entry)
            if len(mesh_messages) > 200:
                del mesh_messages[:-200]

        return {
            "status": "queued",
            "to": to_hash,
            "to_name": entry["to_name"],
            "content": content[:100],
        }

    except Exception as exc:
        log.exception("[mesh] Failed to send LXMF message")
        entry["delivered"] = False
        with messages_lock:
            mesh_messages.append(entry)
            if len(mesh_messages) > 200:
                del mesh_messages[:-200]
        return {"error": str(exc)}


def get_real_topology() -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Build mesh nodes and links from REAL Reticulum data.

    Sources:
      - Our own LXMF destination
      - Discovered peers from LXMF announces
      - RNS.Transport path table for hop counts and interface info
    """
    nodes = []  # type: List[Dict[str, Any]]
    links = []  # type: List[Dict[str, Any]]

    if not mesh_ready or lxmf_destination is None:
        return nodes, links

    our_hash = lxmf_destination.hash.hex()

    # 1. Our own node (always present)
    with discovered_peers_lock:
        peer_count = len(discovered_peers)

    nodes.append({
        "id": our_hash,
        "name": "MeshVision-Mac",
        "type": "local",
        "hop_count": 0,
        "rssi_estimate": 0,
        "last_seen": time.time(),
        "peers_discovered": peer_count,
        "is_transport": False,
        "position": {"azimuth": 0, "distance": 0},
    })

    # 2. Discovered LXMF peers
    with discovered_peers_lock:
        peers_snapshot = dict(discovered_peers)

    for peer_hash_hex, peer_info in peers_snapshot.items():
        hops = -1
        interface_name = "Unknown"
        has_path = False

        try:
            peer_hash_bytes = bytes.fromhex(peer_hash_hex)
            has_path = RNS.Transport.has_path(peer_hash_bytes)
            if has_path:
                hops = RNS.Transport.hops_to(peer_hash_bytes)
                try:
                    next_iface = RNS.Transport.next_hop_interface(peer_hash_bytes)
                    if next_iface:
                        interface_name = getattr(next_iface, 'name', type(next_iface).__name__)
                except Exception:
                    pass
        except Exception:
            pass

        # Update hop count in peer info
        with discovered_peers_lock:
            if peer_hash_hex in discovered_peers:
                discovered_peers[peer_hash_hex]["hops"] = hops

        last_seen = peer_info.get("last_seen", 0)
        azimuth = azimuth_hash(peer_hash_hex)
        distance = max(2.0, (hops + 1) * 3.0) if hops >= 0 else 6.0
        rssi_est = -30 - (max(0, hops) * 15) if hops >= 0 else -75
        is_stale = (time.time() - last_seen) > 600 if last_seen > 0 else True

        nodes.append({
            "id": peer_hash_hex,
            "name": peer_info.get("name", peer_hash_hex[:12]),
            "type": "reticulum",
            "hop_count": hops,
            "rssi_estimate": rssi_est,
            "last_seen": last_seen,
            "peers_discovered": 0,
            "is_transport": False,
            "has_path": has_path,
            "stale": is_stale,
            "position": {"azimuth": azimuth, "distance": distance},
        })

        # Link from us to this peer
        if has_path:
            quality = max(0.1, 0.95 - (max(0, hops) * 0.15))
        elif last_seen > 0:
            quality = 0.3  # announced but no path yet
        else:
            quality = 0.1  # pre-seeded, never seen

        link_key = "{}|{}".format(our_hash, peer_hash_hex)
        history = _update_link_quality(link_key, quality)

        links.append({
            "from": our_hash,
            "to": peer_hash_hex,
            "quality": round(quality, 4),
            "type": "reticulum",
            "medium": interface_name,
            "connected": has_path,
            "quality_history": history,
        })

    return nodes, links


def mesh_management_loop() -> None:
    """Background thread: periodic re-announce + topology refresh."""
    global mesh_nodes, mesh_links
    log.info("[mesh] Mesh management thread started")
    time.sleep(5)  # let mesh settle

    announce_interval = 300  # 5 minutes
    topology_interval = 10   # 10 seconds
    last_announce = 0.0

    while True:
        try:
            now = time.time()

            # Periodic re-announce
            if now - last_announce > announce_interval:
                if lxmf_destination is not None:
                    try:
                        lxmf_destination.announce()
                        log.info("[mesh] Periodic LXMF announce sent")
                    except Exception:
                        log.debug("[mesh] Announce failed", exc_info=True)
                last_announce = now

            # Refresh topology from real data
            if mesh_ready:
                try:
                    nodes, links = get_real_topology()
                    mesh_nodes = nodes
                    mesh_links = links
                except Exception:
                    log.debug("[mesh] Topology refresh error", exc_info=True)

        except Exception:
            log.exception("[mesh] Mesh management loop error")

        time.sleep(topology_interval)


# ---------------------------------------------------------------------------
# ── WiFi Scanner (macOS) ──
# ---------------------------------------------------------------------------
AIRPORT_PATH = (
    "/System/Library/PrivateFrameworks/Apple80211.framework"
    "/Versions/Current/Resources/airport"
)


def _channel_to_freq(channel: int) -> int:
    if 1 <= channel <= 14:
        if channel == 14:
            return 2484
        return 2407 + channel * 5
    elif 36 <= channel <= 177:
        return 5000 + channel * 5
    return 0


def _scan_wifi_corewlan() -> List[Dict[str, Any]]:
    """Scan WiFi using macOS CoreWLAN framework."""
    try:
        import objc
        from Foundation import NSBundle
        bundle = NSBundle.bundleWithPath_('/System/Library/Frameworks/CoreWLAN.framework')
        bundle.load()
        CWWiFiClient = objc.lookUpClass('CWWiFiClient')
        client = CWWiFiClient.sharedWiFiClient()
        iface = client.interface()
        if not iface:
            return []

        networks_set, err = iface.scanForNetworksWithName_error_(None, None)
        if not networks_set:
            return []

        networks = []
        for net in list(networks_set):
            ssid = net.ssid() or "(hidden)"
            bssid = net.bssid() or ""
            rssi = int(net.rssiValue())
            chan_obj = net.wlanChannel()
            channel = int(chan_obj.channelNumber()) if chan_obj else 0
            security = "Open"
            try:
                sec_val = net.security()
                if sec_val > 0:
                    if sec_val & 0x8:
                        security = "WPA2"
                    elif sec_val & 0x4:
                        security = "WPA"
                    elif sec_val & 0x2:
                        security = "WEP"
                    else:
                        security = "Secured"
            except Exception:
                pass

            networks.append({
                "ssid": ssid, "bssid": bssid, "rssi": rssi,
                "channel": channel, "frequency": _channel_to_freq(channel),
                "security": security,
            })
        return networks
    except Exception as exc:
        log.debug("[wifi] CoreWLAN scan error: %s", exc)
        return []


def _scan_wifi_airport() -> List[Dict[str, Any]]:
    """Scan WiFi using the airport command-line tool."""
    try:
        result = subprocess.run(
            [AIRPORT_PATH, "-s"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            return []

        lines = result.stdout.strip().split("\n")
        if len(lines) < 2:
            return []

        header = lines[0]
        bssid_start = header.find("BSSID")
        rssi_start = header.find("RSSI")
        channel_start = header.find("CHANNEL")
        ht_start = header.find("HT")
        security_start = header.find("SECURITY")

        networks = []
        for line in lines[1:]:
            if not line.strip():
                continue
            try:
                ssid = line[:bssid_start].strip()
                bssid = line[bssid_start:rssi_start].strip() if rssi_start > 0 else ""
                rssi_str = line[rssi_start:channel_start].strip() if channel_start > 0 else "0"
                chan_str = line[channel_start:ht_start].strip() if ht_start > 0 else "0"
                security = line[security_start:].strip() if security_start > 0 else "Unknown"

                rssi = int(rssi_str) if rssi_str.lstrip("-").isdigit() else 0
                chan_clean = chan_str.split(",")[0].strip()
                channel = int(chan_clean) if chan_clean.isdigit() else 0

                networks.append({
                    "ssid": ssid if ssid else "(hidden)",
                    "bssid": bssid, "rssi": rssi,
                    "channel": channel, "frequency": _channel_to_freq(channel),
                    "security": security,
                })
            except (ValueError, IndexError):
                continue
        return networks
    except FileNotFoundError:
        return []
    except Exception as exc:
        log.debug("[wifi] airport scan error: %s", exc)
        return []


def _scan_wifi_system_profiler() -> List[Dict[str, Any]]:
    """Fallback WiFi scan using system_profiler."""
    try:
        result = subprocess.run(
            ["system_profiler", "SPAirPortDataType", "-json"],
            capture_output=True, text=True, timeout=20,
        )
        if result.returncode != 0:
            return []

        data = json.loads(result.stdout)
        networks = []
        for item in data.get("SPAirPortDataType", []):
            ifaces = item.get("spairport_airport_interfaces", [])
            for iface in ifaces:
                other_networks = iface.get("spairport_airport_other_local_wireless_networks", [])
                for net in other_networks:
                    ssid = net.get("_name", "(hidden)")
                    bssid = net.get("spairport_network_bssid", "")
                    rssi = net.get("spairport_signal_noise", 0)
                    channel_info = net.get("spairport_network_channel", "0")
                    security = net.get("spairport_security_mode", "Unknown")

                    chan_clean = str(channel_info).split(",")[0].strip()
                    channel = int(chan_clean) if chan_clean.isdigit() else 0

                    networks.append({
                        "ssid": ssid, "bssid": bssid,
                        "rssi": rssi if isinstance(rssi, int) else 0,
                        "channel": channel, "frequency": _channel_to_freq(channel),
                        "security": security,
                    })
        return networks
    except Exception as exc:
        log.debug("[wifi] system_profiler scan error: %s", exc)
        return []


def wifi_scan_loop() -> None:
    """Background thread: full WiFi scan + 10Hz RSSI push_reading between scans."""
    global wifi_results
    log.info("[wifi] Scanner thread started")

    use_corewlan = False
    try:
        test = _scan_wifi_corewlan()
        if test:
            use_corewlan = True
            log.info("[wifi] Using CoreWLAN (%d networks on first scan)", len(test))
    except Exception:
        pass

    if not use_corewlan:
        has_airport = os.path.exists(AIRPORT_PATH)
        log.info("[wifi] Using %s", "airport" if has_airport else "system_profiler fallback")

    while True:
        try:
            if use_corewlan:
                results = _scan_wifi_corewlan()
            elif os.path.exists(AIRPORT_PATH):
                results = _scan_wifi_airport()
            else:
                results = _scan_wifi_system_profiler()
            wifi_results = results
            if env_mapper is not None:
                try:
                    env_mapper.ingest_scan(results)
                except Exception:
                    log.debug("[wifi] env_mapper.ingest_scan error", exc_info=True)
            if results:
                log.debug("[wifi] Scan: %d networks", len(results))
        except Exception:
            log.exception("[wifi] Scan error")

        # Between full scans: rapid 10Hz RSSI reads from connected AP
        if use_corewlan and fast_rssi is not None:
            try:
                import objc
                from Foundation import NSBundle
                bundle = NSBundle.bundleWithPath_('/System/Library/Frameworks/CoreWLAN.framework')
                bundle.load()
                CWWiFiClient = objc.lookUpClass('CWWiFiClient')
                client = CWWiFiClient.sharedWiFiClient()
                iface_fast = client.interface()
                if iface_fast is not None:
                    for _ in range(40):  # 40 reads at 100ms = 4 seconds
                        try:
                            rssi_val = float(iface_fast.rssiValue())
                            noise_val = float(iface_fast.noiseMeasurement())
                            fast_rssi.push_reading(rssi_val, noise_val)
                        except Exception:
                            pass
                        time.sleep(0.1)
                else:
                    time.sleep(4)
            except Exception:
                time.sleep(4)
        else:
            time.sleep(10)


# ---------------------------------------------------------------------------
# ── BLE Scanner ──
# ---------------------------------------------------------------------------
try:
    from bleak import BleakScanner
    HAS_BLEAK = True
except ImportError:
    HAS_BLEAK = False
    log.warning("[ble] bleak not available — BLE scanning disabled")


def ble_scan_loop() -> None:
    """Background thread: scan BLE every 10 seconds."""
    global ble_results
    if not HAS_BLEAK:
        return
    log.info("[ble] Scanner thread started")
    loop = asyncio.new_event_loop()

    async def _scan() -> List[Dict[str, Any]]:
        devices = []
        try:
            scanner = BleakScanner()
            found = await scanner.discover(timeout=5.0)
            for dev in found:
                rssi = getattr(dev, "rssi", None) or -80
                service_uuids = []
                if hasattr(dev, "metadata"):
                    meta = dev.metadata
                    if isinstance(meta, dict):
                        service_uuids = meta.get("uuids", [])
                devices.append({
                    "name": dev.name or "(unknown)",
                    "address": dev.address or "",
                    "rssi": rssi,
                    "services": service_uuids[:5],
                })
        except Exception as exc:
            log.debug("[ble] Scan error: %s", exc)
        return devices

    while True:
        try:
            results = loop.run_until_complete(_scan())
            ble_results = results
        except Exception:
            log.exception("[ble] Scan loop error")
        time.sleep(10)


# ---------------------------------------------------------------------------
# ── Camera / Orientation Estimation ──
# ---------------------------------------------------------------------------
try:
    import cv2
    HAS_CV2 = True
except ImportError:
    HAS_CV2 = False
    log.warning("[camera] OpenCV not available — camera disabled")


def camera_loop() -> None:
    """Background thread: capture camera at ~5fps, estimate orientation."""
    global camera_state, _camera_cumulative_yaw, _camera_reset_heading
    if not HAS_CV2:
        camera_state = {
            "active": False, "yaw_estimate": 0.0, "pitch_estimate": 0.0,
            "heading": 0.0, "angular_velocity": 0.0, "movement_magnitude": 0.0,
        }
        return

    log.info("[camera] Thread starting ...")
    cap = None
    try:
        cap = cv2.VideoCapture(0)
        if not cap.isOpened():
            log.warning("[camera] Could not open VideoCapture(0)")
            camera_state["active"] = False
            return
    except Exception as exc:
        log.warning("[camera] Init failed: %s", exc)
        camera_state["active"] = False
        return

    log.info("[camera] Opened successfully")
    camera_state["active"] = True

    prev_gray = None  # type: Optional[np.ndarray]
    cumulative_yaw = 0.0
    cumulative_pitch = 0.0
    prev_time = time.time()

    lk_params = dict(
        winSize=(15, 15), maxLevel=2,
        criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 10, 0.03),
    )
    feature_params = dict(maxCorners=100, qualityLevel=0.3, minDistance=7, blockSize=7)
    frame_interval = 1.0 / 5.0

    while True:
        try:
            t0 = time.time()
            dt = t0 - prev_time
            prev_time = t0

            if _camera_reset_heading:
                cumulative_yaw = 0.0
                cumulative_pitch = 0.0
                _camera_reset_heading = False
                log.info("[camera] Heading reset to 0")

            ret, frame = cap.read()
            if not ret:
                time.sleep(1)
                continue

            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            small = cv2.resize(gray, (320, 240))

            yaw_delta = 0.0
            pitch_delta = 0.0
            movement_mag = 0.0

            if prev_gray is not None:
                p0 = cv2.goodFeaturesToTrack(prev_gray, mask=None, **feature_params)
                if p0 is not None and len(p0) > 5:
                    p1, status, err = cv2.calcOpticalFlowPyrLK(
                        prev_gray, small, p0, None, **lk_params
                    )
                    if p1 is not None and status is not None:
                        status_flat = status.flatten()
                        good_mask = status_flat == 1
                        good_new = p1[good_mask]
                        good_old = p0[good_mask]

                        if good_new.size > 0:
                            good_new = good_new.reshape(-1, 2)
                        if good_old.size > 0:
                            good_old = good_old.reshape(-1, 2)

                        if (len(good_new) > 3
                                and good_new.ndim == 2 and good_new.shape[1] >= 2
                                and good_old.ndim == 2 and good_old.shape[1] >= 2):
                            dx = float(np.mean(good_new[:, 0] - good_old[:, 0]))
                            dy = float(np.mean(good_new[:, 1] - good_old[:, 1]))
                            yaw_delta = dx * 0.19
                            pitch_delta = dy * 0.19
                            movement_mag = math.sqrt(dx * dx + dy * dy)
                            cumulative_yaw += yaw_delta
                            cumulative_pitch += pitch_delta

            prev_gray = small
            angular_velocity = abs(yaw_delta) / max(dt, 0.001)
            heading = cumulative_yaw % 360
            if heading < 0:
                heading += 360

            with _camera_heading_lock:
                _camera_cumulative_yaw = cumulative_yaw

            camera_state = {
                "active": True,
                "yaw_estimate": round(cumulative_yaw, 2),
                "pitch_estimate": round(cumulative_pitch, 2),
                "heading": round(heading, 2),
                "angular_velocity": round(angular_velocity, 2),
                "movement_magnitude": round(movement_mag, 2),
            }

            elapsed = time.time() - t0
            time.sleep(max(0.01, frame_interval - elapsed))

        except Exception:
            log.exception("[camera] Loop error")
            time.sleep(1)


# ---------------------------------------------------------------------------
# Directional signal enrichment helpers
# ---------------------------------------------------------------------------
def _enrich_wifi_directional(wifi_list: List[Dict[str, Any]], camera_yaw: float) -> List[Dict[str, Any]]:
    enriched = []
    for ap in wifi_list:
        bssid = ap.get("bssid", "")
        az = azimuth_hash(bssid) if bssid else random.randint(0, 359)
        boost = direction_boost(camera_yaw, az)
        entry = dict(ap)
        entry["azimuth_deg"] = az
        entry["direction_boost"] = round(boost, 3)
        enriched.append(entry)
    return enriched


def _enrich_ble_directional(ble_list: List[Dict[str, Any]], camera_yaw: float) -> List[Dict[str, Any]]:
    enriched = []
    for dev in ble_list:
        addr = dev.get("address", "")
        az = azimuth_hash(addr) if addr else random.randint(0, 359)
        boost = direction_boost(camera_yaw, az)
        entry = dict(dev)
        entry["azimuth_deg"] = az
        entry["direction_boost"] = round(boost, 3)
        enriched.append(entry)
    return enriched


def _build_wifi_heatmap(enriched_wifi: List[Dict[str, Any]]) -> Dict[str, int]:
    heatmap = {}  # type: Dict[str, int]
    for i in range(12):
        heatmap[str(i)] = -100
    for ap in enriched_wifi:
        az = ap.get("azimuth_deg", 0)
        rssi = ap.get("rssi", -100)
        sector = int(az / 30) % 12
        key = str(sector)
        if rssi > heatmap[key]:
            heatmap[key] = rssi
    return heatmap


def _build_ble_density(enriched_ble: List[Dict[str, Any]]) -> Dict[str, Any]:
    per_sector = [0] * 12
    for dev in enriched_ble:
        az = dev.get("azimuth_deg", 0)
        sector = int(az / 30) % 12
        per_sector[sector] += 1
    return {"total": len(enriched_ble), "per_sector": per_sector}


# ---------------------------------------------------------------------------
# Groq STT
# ---------------------------------------------------------------------------
def groq_transcribe_audio(audio_b64: str) -> str:
    """Transcribe base64-encoded WAV audio using Groq Whisper STT API."""
    import tempfile
    audio_bytes = base64.b64decode(audio_b64)

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp.write(audio_bytes)
        tmp_path = tmp.name

    try:
        boundary = "----MeshVisionBoundary{}".format(int(time.time() * 1000))
        body_parts = []
        body_parts.append("--{}".format(boundary).encode())
        body_parts.append(b'Content-Disposition: form-data; name="model"')
        body_parts.append(b"")
        body_parts.append(b"whisper-large-v3")
        body_parts.append("--{}".format(boundary).encode())
        body_parts.append('Content-Disposition: form-data; name="file"; filename="audio.wav"'.encode())
        body_parts.append(b"Content-Type: audio/wav")
        body_parts.append(b"")
        body_parts.append(audio_bytes)
        body_parts.append("--{}--".format(boundary).encode())
        body_parts.append(b"")

        body_data = b"\r\n".join(body_parts)

        req = urllib.request.Request(
            GROQ_STT_URL,
            data=body_data,
            method="POST",
            headers={
                "Authorization": "Bearer {}".format(GROQ_API_KEY),
                "Content-Type": "multipart/form-data; boundary={}".format(boundary),
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())
            return result.get("text", "")
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# ── FastAPI Application ──
# ---------------------------------------------------------------------------
app = FastAPI(title="MeshVision Backend", version="3.0.0")


@app.on_event("startup")
async def startup_event() -> None:
    global fast_rssi, env_mapper, presence_est

    log.info("=" * 60)
    log.info("[meshvision] MeshVision Backend v3.0 — Real Mesh Edition")
    log.info("=" * 60)

    # Initialise WiFi-sensing engines
    try:
        fast_rssi = FastRssiMonitor()
        env_mapper = EnvironmentMapper()
        presence_est = HumanPresenceEstimator()
        log.info("[sensing] WiFi-sensing engines initialised")
    except Exception:
        log.exception("[sensing] Failed to initialise sensing engines")

    # Start background scanner threads
    threading.Thread(target=wifi_scan_loop, name="wifi-scanner", daemon=True).start()
    threading.Thread(target=ble_scan_loop, name="ble-scanner", daemon=True).start()
    threading.Thread(target=camera_loop, name="camera", daemon=True).start()

    # Initialize REAL Reticulum mesh — synchronous, blocks until ready
    if HAS_RNS:
        try:
            log.info("[mesh] Initializing real Reticulum mesh (blocking) ...")
            success = init_real_mesh()
            if success:
                log.info("[mesh] Real mesh is LIVE")
                # Start management thread (announces, topology refresh)
                threading.Thread(target=mesh_management_loop, name="mesh-mgmt", daemon=True).start()
            else:
                log.warning("[mesh] Mesh init failed — running without mesh")
        except Exception:
            log.exception("[mesh] Failed to start mesh init thread")
    else:
        log.warning("[mesh] RNS/LXMF not installed — no mesh features")

    # Start WebSocket broadcast task
    asyncio.create_task(_ws_broadcast_loop())

    log.info("[meshvision] Startup complete. Web UI: %s", WEB_DIR)


@app.on_event("shutdown")
async def shutdown_event() -> None:
    global fast_rssi
    log.info("[meshvision] Shutting down ...")
    if fast_rssi:
        try:
            fast_rssi.stop()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# ── Unified payload builder ──
# ---------------------------------------------------------------------------
def build_payload() -> Dict[str, Any]:
    """Build the unified JSON payload for WebSocket clients."""
    # Get current camera heading for directional calculations
    with _camera_heading_lock:
        current_heading = _camera_cumulative_yaw

    # Directional enrichment
    enriched_wifi = _enrich_wifi_directional(list(wifi_results), current_heading)
    enriched_ble = _enrich_ble_directional(list(ble_results), current_heading)
    wifi_heatmap = _build_wifi_heatmap(enriched_wifi)
    ble_density = _build_ble_density(enriched_ble)

    # Messages (last 50)
    with messages_lock:
        msgs = list(mesh_messages[-50:])

    # Shared users
    active_users = get_active_users()

    # WiFi-sensing / presence data
    sensing_data = {}
    if presence_est is not None and fast_rssi is not None and env_mapper is not None:
        try:
            presence_est.update(fast_rssi.get_state(), env_mapper.get_state())
        except Exception:
            log.debug("[sensing] presence_est.update error", exc_info=True)
        try:
            sensing_data = build_sensing_payload(fast_rssi, env_mapper, presence_est)
        except Exception:
            log.debug("[sensing] build_sensing_payload error", exc_info=True)

    return {
        "wifi": enriched_wifi,
        "ble": enriched_ble,
        "mesh_nodes": list(mesh_nodes),
        "mesh_links": list(mesh_links),
        "mesh_messages": msgs,
        "camera": dict(camera_state),
        "wifi_heatmap": wifi_heatmap,
        "ble_density": ble_density,
        "shared_users": active_users,
        "sensing": sensing_data,
        "timestamp": time.time(),
    }


# ---------------------------------------------------------------------------
# ── WebSocket endpoint ──
# ---------------------------------------------------------------------------
async def _ws_broadcast_loop() -> None:
    while True:
        await asyncio.sleep(1.5)
        if not ws_clients:
            continue
        payload = build_payload()
        data = json.dumps(payload)
        dead = set()
        for ws in list(ws_clients):
            try:
                await ws.send_text(data)
            except Exception:
                dead.add(ws)
        ws_clients.difference_update(dead)


@app.websocket("/ws/mesh")
async def websocket_mesh(websocket: WebSocket) -> None:
    global _camera_reset_heading

    await websocket.accept()
    ws_clients.add(websocket)
    log.info("[ws] Client connected (%d total)", len(ws_clients))
    try:
        # Send initial payload immediately
        payload = build_payload()
        await websocket.send_text(json.dumps(payload))

        while True:
            try:
                data = await asyncio.wait_for(websocket.receive_text(), timeout=30)
                try:
                    cmd = json.loads(data)
                    cmd_type = cmd.get("type", "")

                    if cmd_type == "reset_heading":
                        _camera_reset_heading = True
                        log.info("[ws] Heading reset requested")

                    elif cmd_type == "broadcast":
                        broadcast_msg = {
                            "type": "broadcast",
                            "from_user": cmd.get("from_user", "anonymous"),
                            "content": cmd.get("content", ""),
                            "timestamp": time.time(),
                        }
                        broadcast_json = json.dumps(broadcast_msg)
                        for ws_other in list(ws_clients):
                            try:
                                await ws_other.send_text(broadcast_json)
                            except Exception:
                                pass

                    elif cmd_type == "register_user":
                        register_user(
                            name=cmd.get("name", "unknown"),
                            lat=float(cmd.get("lat", 0)),
                            lon=float(cmd.get("lon", 0)),
                            heading=float(cmd.get("heading", 0)),
                        )

                except (json.JSONDecodeError, ValueError, TypeError):
                    pass

            except asyncio.TimeoutError:
                try:
                    await websocket.send_text(json.dumps({"ping": True}))
                except Exception:
                    break
    except WebSocketDisconnect:
        pass
    except Exception:
        log.debug("[ws] Error (client likely disconnected)")
    finally:
        ws_clients.discard(websocket)
        log.info("[ws] Client disconnected (%d remaining)", len(ws_clients))


# ---------------------------------------------------------------------------
# ── REST Endpoints ──
# ---------------------------------------------------------------------------

@app.get("/api/status")
async def api_status() -> JSONResponse:
    """Health check / status."""
    our_hash = ""
    interfaces_info = []
    if lxmf_destination is not None:
        our_hash = lxmf_destination.hash.hex()
    if HAS_RNS and mesh_ready:
        try:
            for iface in RNS.Transport.interfaces:
                interfaces_info.append({
                    "name": getattr(iface, 'name', '?'),
                    "type": type(iface).__name__,
                    "online": getattr(iface, 'online', True),
                })
        except Exception:
            pass

    with discovered_peers_lock:
        peer_count = len(discovered_peers)

    return JSONResponse(content={
        "status": "ok",
        "version": "3.0.0-real-mesh",
        "mesh_ready": mesh_ready,
        "rns_available": HAS_RNS,
        "our_lxmf_hash": our_hash,
        "discovered_peers": peer_count,
        "interfaces": interfaces_info,
        "bleak_available": HAS_BLEAK,
        "opencv_available": HAS_CV2,
        "wifi_count": len(wifi_results),
        "ble_count": len(ble_results),
        "camera_active": camera_state.get("active", False),
        "ws_clients": len(ws_clients),
        "shared_users": len(shared_users),
        "sensing_active": fast_rssi is not None,
        "groq_configured": bool(GROQ_API_KEY),
        "uptime": time.time(),
    })


@app.get("/api/nodes")
async def api_nodes() -> JSONResponse:
    """Return current mesh node info (REAL topology)."""
    return JSONResponse(content={"nodes": list(mesh_nodes)})


@app.get("/api/topology")
async def api_topology() -> JSONResponse:
    """Return mesh topology (REAL nodes + links)."""
    return JSONResponse(content={
        "nodes": list(mesh_nodes),
        "links": list(mesh_links),
    })


@app.post("/api/send-message")
async def api_send_message(request: Request) -> JSONResponse:
    """Send a REAL LXMF message over the Reticulum mesh.

    Body: {"content": "hello", "to_hash": "1190da39b618..."}
    If to_hash omitted, message is recorded locally only.
    """
    if not mesh_ready:
        return JSONResponse(status_code=503, content={"error": "Mesh not ready"})

    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Invalid JSON body"})

    content = body.get("content", "")
    to_hash = body.get("to_hash", body.get("to", ""))

    if not content:
        return JSONResponse(status_code=400, content={"error": "Missing 'content' field"})

    # Send in a thread to avoid blocking
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, send_lxmf_message, content, to_hash if to_hash else None)

    if "error" in result:
        return JSONResponse(status_code=400, content=result)
    return JSONResponse(content=result)


@app.get("/api/messages")
async def api_messages() -> JSONResponse:
    """Return mesh messages (REAL LXMF messages)."""
    with messages_lock:
        msgs = list(mesh_messages[-50:])
    return JSONResponse(content={"messages": msgs})


@app.get("/api/wifi")
async def api_wifi() -> JSONResponse:
    with _camera_heading_lock:
        heading = _camera_cumulative_yaw
    enriched = _enrich_wifi_directional(list(wifi_results), heading)
    return JSONResponse(content={"wifi": enriched})


@app.get("/api/ble")
async def api_ble() -> JSONResponse:
    with _camera_heading_lock:
        heading = _camera_cumulative_yaw
    enriched = _enrich_ble_directional(list(ble_results), heading)
    return JSONResponse(content={"ble": enriched})


@app.get("/api/camera")
async def api_camera() -> JSONResponse:
    return JSONResponse(content={"camera": dict(camera_state)})


@app.get("/api/wifi-heatmap")
async def api_wifi_heatmap() -> JSONResponse:
    with _camera_heading_lock:
        heading = _camera_cumulative_yaw
    enriched = _enrich_wifi_directional(list(wifi_results), heading)
    return JSONResponse(content={"wifi_heatmap": _build_wifi_heatmap(enriched)})


@app.get("/api/ble-density")
async def api_ble_density() -> JSONResponse:
    with _camera_heading_lock:
        heading = _camera_cumulative_yaw
    enriched = _enrich_ble_directional(list(ble_results), heading)
    return JSONResponse(content={"ble_density": _build_ble_density(enriched)})


@app.get("/api/shared-users")
async def api_shared_users() -> JSONResponse:
    return JSONResponse(content={"shared_users": get_active_users()})


@app.get("/api/link-history")
async def api_link_history() -> JSONResponse:
    with link_quality_lock:
        result = {}
        for key, hist in link_quality_history.items():
            result[key] = [[round(t, 2), round(q, 4)] for t, q in hist[-30:]]
    return JSONResponse(content={"link_history": result})


# ---------------------------------------------------------------------------
# WiFi-sensing / Presence / Environment REST endpoints
# ---------------------------------------------------------------------------

@app.get("/api/vital-signs")
async def api_vital_signs() -> JSONResponse:
    if fast_rssi is None:
        return JSONResponse(content={"error": "FastRssiMonitor not initialised"}, status_code=503)
    try:
        state = fast_rssi.get_state()
        return JSONResponse(content={
            "vital_signs": {
                "breathing": state.get("breathing"),
                "heart_rate": state.get("heart_rate"),
                "motion": state.get("motion"),
                "presence": state.get("presence"),
            },
            "timestamp": time.time(),
        })
    except Exception as exc:
        return JSONResponse(content={"error": str(exc)}, status_code=500)


@app.get("/api/wifi-sensing")
async def api_wifi_sensing() -> JSONResponse:
    if fast_rssi is None:
        return JSONResponse(content={"error": "FastRssiMonitor not initialised"}, status_code=503)
    try:
        return JSONResponse(content={
            "wifi_sensing": fast_rssi.get_state(),
            "timestamp": time.time(),
        })
    except Exception as exc:
        return JSONResponse(content={"error": str(exc)}, status_code=500)


@app.get("/api/presence")
async def api_presence() -> JSONResponse:
    if presence_est is None:
        return JSONResponse(content={"error": "HumanPresenceEstimator not initialised"}, status_code=503)
    try:
        if fast_rssi is not None and env_mapper is not None:
            presence_est.update(fast_rssi.get_state(), env_mapper.get_state())
        data = presence_est.get_state()
        return JSONResponse(content={"presence": data, "timestamp": time.time()})
    except Exception as exc:
        return JSONResponse(content={"error": str(exc)}, status_code=500)


@app.get("/api/environment")
async def api_environment() -> JSONResponse:
    if env_mapper is None:
        return JSONResponse(content={"error": "EnvironmentMapper not initialised"}, status_code=503)
    try:
        return JSONResponse(content={"environment": env_mapper.get_state(), "timestamp": time.time()})
    except Exception as exc:
        return JSONResponse(content={"error": str(exc)}, status_code=500)


# ---------------------------------------------------------------------------
# Multi-user REST endpoints
# ---------------------------------------------------------------------------

@app.post("/api/register-user")
async def api_register_user(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Invalid JSON body"})
    name = body.get("name", "")
    if not name:
        return JSONResponse(status_code=400, content={"error": "Missing 'name' field"})
    register_user(name, float(body.get("lat", 0)), float(body.get("lon", 0)), float(body.get("heading", 0)))
    return JSONResponse(content={"status": "ok", "name": name})


@app.post("/api/broadcast")
async def api_broadcast(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Invalid JSON body"})
    from_user = body.get("from_user", "anonymous")
    content = body.get("content", "")
    if not content:
        return JSONResponse(status_code=400, content={"error": "Missing 'content' field"})
    broadcast_msg = {
        "type": "broadcast", "from_user": from_user,
        "content": content, "timestamp": time.time(),
    }
    broadcast_json = json.dumps(broadcast_msg)
    sent_count = 0
    for ws in list(ws_clients):
        try:
            await ws.send_text(broadcast_json)
            sent_count += 1
        except Exception:
            pass
    return JSONResponse(content={"status": "ok", "sent_to": sent_count})


# ---------------------------------------------------------------------------
# Voice Chat (Groq STT -> LXMF)
# ---------------------------------------------------------------------------

@app.post("/api/voice-chat")
async def api_voice_chat(request: Request) -> JSONResponse:
    """Accept audio, transcribe with Groq STT, send as real LXMF message."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Invalid JSON"})

    audio_b64 = body.get("audio", "")
    dest_hash = body.get("destination_hash", body.get("to_hash", ""))

    if not audio_b64:
        return JSONResponse(status_code=400, content={"error": "Missing 'audio' (base64 WAV)"})
    if not GROQ_API_KEY:
        return JSONResponse(status_code=503, content={"error": "GROQ_API_KEY not configured"})

    loop = asyncio.get_event_loop()
    try:
        transcription = await loop.run_in_executor(None, groq_transcribe_audio, audio_b64)
    except Exception as exc:
        log.exception("[voice] Groq STT failed")
        return JSONResponse(status_code=502, content={"error": "Transcription failed: {}".format(str(exc))})

    if not transcription or not transcription.strip():
        return JSONResponse(content={"transcription": "", "status": "empty"})

    log.info("[voice] Transcription: %s", transcription[:80])

    # Send as real LXMF message if destination provided
    if dest_hash and mesh_ready:
        result = await loop.run_in_executor(None, send_lxmf_message, transcription, dest_hash)
        status = "lxmf_queued" if "error" not in result else "lxmf_failed"
    else:
        # Record locally
        with messages_lock:
            mesh_messages.append({
                "from_name": "MeshVision-Mac (voice)",
                "from_hash": lxmf_destination.hash.hex() if lxmf_destination else "",
                "to_name": "local",
                "content": transcription,
                "timestamp": time.time(),
                "hops": 0,
                "delivered": False,
                "source": "voice_local",
            })
            if len(mesh_messages) > 200:
                del mesh_messages[:-200]
        status = "local_only"

    return JSONResponse(content={"transcription": transcription, "status": status})


# ---------------------------------------------------------------------------
# Mesh Chat Status (Pi reachability over real mesh)
# ---------------------------------------------------------------------------

@app.get("/api/mesh-chat/status")
async def api_mesh_chat_status() -> JSONResponse:
    """Check if Pi MeshChat is reachable over the REAL Reticulum mesh."""
    if not mesh_ready or not HAS_RNS:
        return JSONResponse(content={"pi_meshchat": "mesh_not_ready"})

    try:
        pi_hash_bytes = bytes.fromhex(PI_LXMF_HASH)
        has_path = RNS.Transport.has_path(pi_hash_bytes)
        hops = -1
        if has_path:
            try:
                hops = RNS.Transport.hops_to(pi_hash_bytes)
            except Exception:
                pass

        # Check if we've seen an announce from the Pi
        with discovered_peers_lock:
            pi_peer = discovered_peers.get(PI_LXMF_HASH, {})
        pi_last_seen = pi_peer.get("last_seen", 0)
        pi_name = pi_peer.get("name", "Pi-MeshChat")
        identity_recalled = RNS.Identity.recall(pi_hash_bytes) is not None

        if has_path and identity_recalled:
            status = "online"
        elif has_path:
            status = "path_only"  # have path but no identity (can't send yet)
        elif pi_last_seen > 0:
            status = "announced"  # seen announce but no current path
        else:
            status = "offline"

        return JSONResponse(content={
            "pi_meshchat": status,
            "pi_lxmf_hash": PI_LXMF_HASH,
            "has_path": has_path,
            "hops": hops,
            "last_seen": pi_last_seen,
            "display_name": pi_name,
            "identity_recalled": identity_recalled,
        })
    except Exception as exc:
        return JSONResponse(content={"pi_meshchat": "error", "detail": str(exc)})


@app.get("/api/mesh-chat/messages")
async def api_mesh_chat_messages(request: Request) -> JSONResponse:
    """Return mesh messages since a given timestamp."""
    since = float(request.query_params.get("since", "0"))
    with messages_lock:
        msgs = [m for m in mesh_messages if m.get("timestamp", 0) > since]
    return JSONResponse(content={"messages": msgs[-50:]})


# ---------------------------------------------------------------------------
# Discovered peers list endpoint
# ---------------------------------------------------------------------------

@app.get("/api/peers")
async def api_peers() -> JSONResponse:
    """Return all discovered LXMF peers."""
    with discovered_peers_lock:
        peers = []
        for hex_hash, info in discovered_peers.items():
            peers.append({
                "hash": hex_hash,
                "name": info.get("name", hex_hash[:12]),
                "last_seen": info.get("last_seen", 0),
                "hops": info.get("hops", -1),
            })
    return JSONResponse(content={"peers": peers})


# ---------------------------------------------------------------------------
# ── Serve static files (web UI) at root ──
# ---------------------------------------------------------------------------
_index_path = WEB_DIR / "index.html"
if not _index_path.exists():
    WEB_DIR.mkdir(parents=True, exist_ok=True)
    _index_path.write_text(
        "<!DOCTYPE html><html><head><title>MeshVision HUD</title></head>"
        "<body><h1>MeshVision HUD</h1><p>Web UI placeholder. "
        "Connect via WebSocket at <code>/ws/mesh</code></p>"
        "<pre id='data'></pre>"
        "<script>"
        "const ws=new WebSocket(`ws://${location.host}/ws/mesh`);"
        "ws.onmessage=e=>{document.getElementById('data').textContent="
        "JSON.stringify(JSON.parse(e.data),null,2)};"
        "</script></body></html>",
        encoding="utf-8",
    )
    log.info("[meshvision] Created placeholder index.html at %s", _index_path)

app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="static")


# ---------------------------------------------------------------------------
# ── Main entry point ──
# ---------------------------------------------------------------------------
def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="MeshVision Backend Server v3.0 — Real Mesh")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8420, help="Bind port (default: 8420)")
    parser.add_argument("--reload", action="store_true", help="Enable auto-reload")
    args = parser.parse_args()

    log.info("[meshvision] Starting MeshVision v3.0 on %s:%d", args.host, args.port)
    uvicorn.run(
        "server:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )


if __name__ == "__main__":
    main()
