#!/usr/bin/env python3
"""Quick test of CoreWLAN scan data available."""
import time, objc
from Foundation import NSBundle

b = NSBundle.bundleWithPath_('/System/Library/Frameworks/CoreWLAN.framework')
b.load()
CWWiFiClient = objc.lookUpClass('CWWiFiClient')
client = CWWiFiClient.sharedWiFiClient()
iface = client.interface()

print("Connected RSSI:", iface.rssiValue(), "Noise:", iface.noiseMeasurement())
t0 = time.time()
nets, err = iface.scanForNetworksWithName_error_(None, None)
dt = time.time() - t0
nets_list = list(nets) if nets else []
print(f"Scan: {len(nets_list)} APs in {dt*1000:.0f}ms")
for n in sorted(nets_list, key=lambda x: x.rssiValue(), reverse=True)[:10]:
    ch = n.wlanChannel().channelNumber() if n.wlanChannel() else 0
    bw = n.wlanChannel().channelWidth() if n.wlanChannel() else 0
    print(f"  ch{ch:3d} bw{bw} RSSI={n.rssiValue():4d} {str(n.ssid() or '(hidden)')[:25]}")

# Test: can we get RSSI of CONNECTED network without a full scan?
print("\n--- Rapid RSSI reads of connected AP (no scan) ---")
for i in range(10):
    print(f"  rssi={iface.rssiValue()} noise={iface.noiseMeasurement()}")
    time.sleep(0.1)
