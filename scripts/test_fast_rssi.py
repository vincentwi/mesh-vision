#!/usr/bin/env python3
"""Test FastRssiMonitor directly."""
import time
import sys
sys.path.insert(0, '/Users/vinceroy/Desktop/APP/Glasses/mesh-vision/backend')
import logging
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s %(levelname)s %(name)s: %(message)s')

from wifi_sensing import FastRssiMonitor

m = FastRssiMonitor()
ok = m.start()
print(f"start() returned: {ok}")
time.sleep(3)
state = m.get_state()
print(f"sample_count: {state['sample_count']}")
print(f"current_rssi: {state['current_rssi']}")
print(f"sparkline len: {len(state['rssi_sparkline'])}")
if state['rssi_sparkline']:
    print(f"sparkline[-5:]: {state['rssi_sparkline'][-5:]}")
m.stop()
