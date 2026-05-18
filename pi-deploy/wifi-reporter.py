#!/usr/bin/env python3
"""
MeshVision WiFi Spectrum Reporter
Runs on Raspberry Pi, scans WiFi environment, reports to MeshVision backend.
Also reports Reticulum mesh node status.

Designed for Python 3.9+ on Raspberry Pi OS.
"""

import json
import logging
import os
import platform
import re
import subprocess
import socket
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# --- Configuration ---
SCAN_INTERVAL = 30          # seconds between scans
BACKEND_HOST = os.environ.get("MESHVISION_BACKEND", "")
BACKEND_PORT = int(os.environ.get("MESHVISION_PORT", "3001"))
REPORT_ENDPOINT = "/api/pi-report"
NODE_NAME = os.environ.get("MESHVISION_NODE_NAME", socket.gethostname())
WIFI_INTERFACE = os.environ.get("MESHVISION_WIFI_IF", "wlan0")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [WiFiReporter] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("wifi-reporter")


def get_default_gateway_ip() -> Optional[str]:
    """Discover the gateway IP (likely the Mac running MeshVision)."""
    try:
        result = subprocess.run(
            ["ip", "route", "show", "default"],
            capture_output=True, text=True, timeout=5,
        )
        # default via 10.0.10.1 dev wlan0 ...
        match = re.search(r"default via (\S+)", result.stdout)
        if match:
            return match.group(1)
    except Exception:
        pass
    return None


def discover_backend() -> str:
    """Try to discover the MeshVision backend address."""
    if BACKEND_HOST:
        return f"http://{BACKEND_HOST}:{BACKEND_PORT}"

    # Try gateway IP (common for pi-star setups where Mac is gateway)
    gw = get_default_gateway_ip()
    if gw:
        return f"http://{gw}:{BACKEND_PORT}"

    # Fallback: try common addresses on pi-star network
    for candidate in ["10.0.10.1", "10.0.10.100", "10.0.10.2"]:
        try:
            sock = socket.create_connection((candidate, BACKEND_PORT), timeout=2)
            sock.close()
            return f"http://{candidate}:{BACKEND_PORT}"
        except (socket.timeout, ConnectionRefusedError, OSError):
            continue

    return f"http://10.0.10.1:{BACKEND_PORT}"


def get_pi_info() -> dict:
    """Gather basic Pi system information."""
    info = {
        "hostname": socket.gethostname(),
        "node_name": NODE_NAME,
        "platform": platform.machine(),
        "os": "",
        "uptime": 0,
        "ip_address": "",
        "cpu_temp": None,
    }

    # OS info
    try:
        with open("/etc/os-release") as f:
            for line in f:
                if line.startswith("PRETTY_NAME="):
                    info["os"] = line.split("=", 1)[1].strip().strip('"')
                    break
    except FileNotFoundError:
        info["os"] = platform.platform()

    # Uptime
    try:
        with open("/proc/uptime") as f:
            info["uptime"] = int(float(f.read().split()[0]))
    except Exception:
        pass

    # IP address
    try:
        result = subprocess.run(
            ["hostname", "-I"], capture_output=True, text=True, timeout=5,
        )
        info["ip_address"] = result.stdout.strip().split()[0] if result.stdout.strip() else ""
    except Exception:
        pass

    # CPU temperature
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            info["cpu_temp"] = round(int(f.read().strip()) / 1000.0, 1)
    except Exception:
        pass

    return info


def scan_wifi_iw() -> list:
    """Scan WiFi using 'iw' command (preferred, more detail)."""
    aps = []
    try:
        result = subprocess.run(
            ["sudo", "iw", "dev", WIFI_INTERFACE, "scan"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            log.warning("iw scan failed: %s", result.stderr.strip())
            return aps

        current_ap = {}
        for line in result.stdout.splitlines():
            line = line.strip()

            if line.startswith("BSS "):
                if current_ap:
                    aps.append(current_ap)
                bssid = line.split()[1].split("(")[0]
                current_ap = {
                    "bssid": bssid,
                    "ssid": "",
                    "signal_dbm": 0,
                    "frequency_mhz": 0,
                    "channel": 0,
                    "security": "Open",
                }
            elif line.startswith("SSID:"):
                current_ap["ssid"] = line[5:].strip()
            elif line.startswith("signal:"):
                match = re.search(r"(-?\d+\.?\d*)\s*dBm", line)
                if match:
                    current_ap["signal_dbm"] = float(match.group(1))
            elif line.startswith("freq:"):
                try:
                    freq = int(line.split(":")[1].strip())
                    current_ap["frequency_mhz"] = freq
                    current_ap["channel"] = freq_to_channel(freq)
                except (ValueError, IndexError):
                    pass
            elif "WPA" in line or "RSN" in line:
                current_ap["security"] = "WPA2/WPA3"
            elif "WEP" in line:
                current_ap["security"] = "WEP"

        if current_ap:
            aps.append(current_ap)

    except FileNotFoundError:
        log.debug("iw not available")
    except subprocess.TimeoutExpired:
        log.warning("iw scan timed out")
    except Exception as e:
        log.warning("iw scan error: %s", e)

    return aps


def scan_wifi_iwlist() -> list:
    """Fallback: scan WiFi using 'iwlist' command."""
    aps = []
    try:
        result = subprocess.run(
            ["sudo", "iwlist", WIFI_INTERFACE, "scan"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return aps

        current_ap = {}
        for line in result.stdout.splitlines():
            line = line.strip()

            if "Cell" in line and "Address:" in line:
                if current_ap:
                    aps.append(current_ap)
                bssid = line.split("Address:")[1].strip()
                current_ap = {
                    "bssid": bssid,
                    "ssid": "",
                    "signal_dbm": 0,
                    "frequency_mhz": 0,
                    "channel": 0,
                    "security": "Open",
                }
            elif "ESSID:" in line:
                match = re.search(r'ESSID:"(.+?)"', line)
                if match:
                    current_ap["ssid"] = match.group(1)
            elif "Signal level=" in line:
                match = re.search(r"Signal level[=:](-?\d+)", line)
                if match:
                    current_ap["signal_dbm"] = int(match.group(1))
            elif "Frequency:" in line:
                match = re.search(r"Frequency:(\d+\.?\d*)\s*GHz", line)
                if match:
                    freq_ghz = float(match.group(1))
                    freq_mhz = int(freq_ghz * 1000)
                    current_ap["frequency_mhz"] = freq_mhz
                    current_ap["channel"] = freq_to_channel(freq_mhz)
            elif "Channel:" in line:
                match = re.search(r"Channel:(\d+)", line)
                if match:
                    current_ap["channel"] = int(match.group(1))
            elif "WPA" in line:
                current_ap["security"] = "WPA2/WPA3"
            elif "WEP" in line:
                current_ap["security"] = "WEP"

        if current_ap:
            aps.append(current_ap)

    except Exception as e:
        log.warning("iwlist scan error: %s", e)

    return aps


def freq_to_channel(freq_mhz: int) -> int:
    """Convert WiFi frequency (MHz) to channel number."""
    if 2412 <= freq_mhz <= 2484:
        if freq_mhz == 2484:
            return 14
        return (freq_mhz - 2407) // 5
    elif 5170 <= freq_mhz <= 5825:
        return (freq_mhz - 5000) // 5
    elif 5955 <= freq_mhz <= 7115:
        return (freq_mhz - 5950) // 5
    return 0


def scan_wifi() -> list:
    """Scan WiFi using best available method."""
    aps = scan_wifi_iw()
    if not aps:
        aps = scan_wifi_iwlist()
    return aps


def get_reticulum_status() -> dict:
    """Get Reticulum daemon status."""
    status = {
        "running": False,
        "transport_enabled": False,
        "interfaces": [],
        "peers": 0,
        "announces": 0,
    }

    # Check if rnsd is running
    try:
        result = subprocess.run(
            ["pgrep", "-x", "rnsd"],
            capture_output=True, text=True, timeout=5,
        )
        status["running"] = result.returncode == 0
    except Exception:
        pass

    # Get rnstatus output
    try:
        result = subprocess.run(
            ["rnstatus", "-j"],  # JSON output if available
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            try:
                data = json.loads(result.stdout)
                status["transport_enabled"] = data.get("transport_enabled", False)
                status["interfaces"] = [
                    {
                        "name": iface.get("name", "unknown"),
                        "type": iface.get("type", "unknown"),
                        "status": iface.get("status", "unknown"),
                        "peers": iface.get("peers", 0),
                    }
                    for iface in data.get("interfaces", [])
                ]
            except json.JSONDecodeError:
                # Parse text output as fallback
                status["transport_enabled"] = "Transport" in result.stdout
                for line in result.stdout.splitlines():
                    if "interface" in line.lower() or "Interface" in line:
                        status["interfaces"].append({"name": line.strip(), "status": "active"})
    except FileNotFoundError:
        log.debug("rnstatus not found")
    except Exception as e:
        log.debug("rnstatus error: %s", e)

    return status


def send_report(backend_url: str, report: dict) -> bool:
    """POST report to MeshVision backend."""
    url = f"{backend_url}{REPORT_ENDPOINT}"
    payload = json.dumps(report).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "User-Agent": f"MeshVision-PiReporter/{NODE_NAME}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                log.info("Report sent → %s (%d APs, Reticulum=%s)",
                         url, len(report.get("wifi_aps", [])),
                         "up" if report.get("reticulum", {}).get("running") else "down")
                return True
            else:
                log.warning("Backend returned %d", resp.status)
                return False
    except urllib.error.URLError as e:
        log.warning("Cannot reach backend at %s: %s", url, e.reason)
        return False
    except Exception as e:
        log.warning("Report failed: %s", e)
        return False


def main():
    log.info("MeshVision WiFi Reporter starting")
    log.info("Node: %s | Interface: %s | Interval: %ds", NODE_NAME, WIFI_INTERFACE, SCAN_INTERVAL)

    backend_url = discover_backend()
    log.info("Backend: %s", backend_url)

    consecutive_failures = 0
    max_failures_before_rediscover = 5

    while True:
        try:
            # Gather all data
            pi_info = get_pi_info()
            wifi_aps = scan_wifi()
            rns_status = get_reticulum_status()

            # Build report
            report = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "node": pi_info,
                "wifi_aps": wifi_aps,
                "wifi_summary": {
                    "total_aps": len(wifi_aps),
                    "unique_ssids": len(set(ap.get("ssid", "") for ap in wifi_aps if ap.get("ssid"))),
                    "channels_2g": sorted(set(
                        ap["channel"] for ap in wifi_aps
                        if 1 <= ap.get("channel", 0) <= 14
                    )),
                    "channels_5g": sorted(set(
                        ap["channel"] for ap in wifi_aps
                        if ap.get("channel", 0) > 14
                    )),
                    "strongest_signal": max(
                        (ap.get("signal_dbm", -100) for ap in wifi_aps),
                        default=-100,
                    ),
                },
                "reticulum": rns_status,
            }

            # Send to backend
            success = send_report(backend_url, report)

            if success:
                consecutive_failures = 0
            else:
                consecutive_failures += 1
                if consecutive_failures >= max_failures_before_rediscover:
                    log.info("Rediscovering backend after %d failures...", consecutive_failures)
                    backend_url = discover_backend()
                    log.info("New backend: %s", backend_url)
                    consecutive_failures = 0

        except KeyboardInterrupt:
            log.info("Shutting down")
            break
        except Exception as e:
            log.error("Scan cycle error: %s", e, exc_info=True)

        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    main()
