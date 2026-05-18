#!/usr/bin/env python3
"""Test init_real_mesh from server.py directly."""
import sys, os, logging
logging.basicConfig(level=logging.DEBUG, stream=sys.stderr)
os.chdir('/Users/vinceroy/Desktop/APP/Glasses/mesh-vision/backend')
sys.path.insert(0, '.')
from server import init_real_mesh
result = init_real_mesh()
print(f'Result: {result}')
