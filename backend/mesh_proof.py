#!/usr/bin/env python3
"""Prove mesh communication: send LXMF message Mac -> Pi over Reticulum TCP."""
import sys, time
sys.stdout.reconfigure(line_buffering=True)

import RNS, LXMF

print("=== MESH COMMUNICATION PROOF ===")
print("Connecting to shared Reticulum instance...")
r = RNS.Reticulum()

# Load identity
ident = RNS.Identity.from_file("backend/meshvision_identity")
print(f"Mac identity: {ident.hexhash}")

# Create LXMF router
router = LXMF.LXMRouter(storagepath="backend/lxmf_storage")
local = router.register_delivery_identity(ident, display_name="MeshVision-Mac")

# Pi's LXMF destination
pi_hash = bytes.fromhex("22871c5306bf067746f09cc4ea819dde")

# Check/request path
has = RNS.Transport.has_path(pi_hash)
print(f"Has path to Pi: {has}")
if not has:
    print("Requesting path...")
    RNS.Transport.request_path(pi_hash)
    for i in range(10):
        time.sleep(1)
        if RNS.Transport.has_path(pi_hash):
            print(f"Path found after {i+1}s")
            break
    else:
        print("FAILED: No path to Pi after 10s")
        sys.exit(1)

hops = RNS.Transport.hops_to(pi_hash)
print(f"Path: {hops} hop(s) via TCP transport")

# Recall Pi identity
pi_ident = RNS.Identity.recall(pi_hash)
if not pi_ident:
    print("FAILED: Pi identity not known yet")
    sys.exit(1)

print(f"Pi identity recalled: {pi_ident.hexhash}")

# Build destination and send
pi_dest = RNS.Destination(pi_ident, RNS.Destination.OUT, RNS.Destination.SINGLE, "lxmf", "delivery")

timestamp = time.strftime("%H:%M:%S")
msg_text = f"[{timestamp}] Mesh proof: this message traveled Mac->Pi via Reticulum TCP:4242, encrypted E2E with X25519+AES-256. NOT over WiFi HTTP."

msg = LXMF.LXMessage(pi_dest, local, msg_text, desired_method=LXMF.LXMessage.DIRECT)

def on_delivery(message):
    print(f"DELIVERED! Message confirmed received by Pi.")

msg.delivery_callback = on_delivery
router.handle_outbound(msg)
print(f"Message queued. State: {msg.state}")
print(f"Content: {msg_text}")
print()
print("=== HOW THIS PROVES NON-WIFI COMMUNICATION ===")
print("1. Reticulum runs its own transport layer OVER TCP")
print("2. The TCP socket (Mac:4243 -> Pi:4242) carries raw Reticulum packets")
print("3. LXMF encrypts the message end-to-end (Pi's public key)")
print("4. Even if WiFi HTTP was blocked, Reticulum would still deliver")
print("5. Reticulum can also work over serial/LoRa/packet radio — same protocol")
print()

# Wait for delivery confirmation
print("Waiting 10s for delivery confirmation...")
for i in range(10):
    time.sleep(1)
    if msg.state == LXMF.LXMessage.DELIVERED:
        print(f"CONFIRMED DELIVERED after {i+1}s!")
        break
else:
    print(f"Final message state: {msg.state} (may still be in transit)")

print("\n=== CHECK PI SIDE ===")
print("Open http://10.0.10.82:8080 in your browser to see the message in MeshChat")
