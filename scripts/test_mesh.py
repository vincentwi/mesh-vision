#!/usr/bin/env python3
"""Test real mesh init."""
import sys
sys.path.insert(0, '/Users/vinceroy/Desktop/APP/Glasses/mesh-vision/backend')
import RNS, LXMF, logging
logging.basicConfig(level=logging.DEBUG)

print("Connecting to shared Reticulum...")
r = RNS.Reticulum()
print("Connected!")
print("Identity...")
i = RNS.Identity()
print("LXMF Router...")
router = LXMF.LXMRouter(identity=i, storagepath='/tmp/test_lxmf')
dest = router.register_delivery_identity(i, display_name='test')
print(f"Our LXMF hash: {dest.hash.hex()}")
dest.announce()
print("Announced! Waiting 5s for peers...")
import time; time.sleep(5)
print("Done")
