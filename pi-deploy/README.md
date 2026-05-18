# MeshVision Raspberry Pi Deployment

Deploys a complete Reticulum mesh node with WiFi spectrum scanning to a Raspberry Pi.

## What Gets Installed

| Component | Description | Port |
|-----------|-------------|------|
| **rnsd** | Reticulum daemon — transport relay node | TCP 4242 |
| **MeshChat** | Web-based LXMF messaging UI | HTTP 8080 |
| **NomadNet** | Terminal-based encrypted mesh comms | — |
| **WiFi Reporter** | Scans WiFi spectrum, reports to MeshVision | — |

## Quick Deploy

From your Mac (must be on same network as the Pi):

```bash
cd mesh-vision/pi-deploy
chmod +x deploy.sh
./deploy.sh pi@10.0.10.82
```

Or with password:

```bash
./deploy.sh pi@10.0.10.82 raspberry
```

If using password auth, install sshpass first:

```bash
brew install hudochenkov/sshpass/sshpass
```

## Manual Install

If you prefer to install manually:

```bash
# Copy files to Pi
scp -r pi-deploy/ pi@10.0.10.82:/tmp/mesh-vision/

# SSH to Pi and run installer
ssh pi@10.0.10.82
sudo bash /tmp/mesh-vision/install.sh
```

## Files

| File | Purpose |
|------|---------|
| `deploy.sh` | Run from Mac — uploads files and runs installer |
| `install.sh` | Master installer — runs on the Pi |
| `reticulum-config` | Reticulum config (transport node, TCP:4242) |
| `rnsd.service` | Systemd unit for Reticulum daemon |
| `meshchat.service` | Systemd unit for MeshChat web UI |
| `wifi-reporter.py` | WiFi scanner + Reticulum status reporter |
| `wifi-reporter.service` | Systemd unit for WiFi reporter |

## After Deployment

### Connect Mac to Pi's Reticulum

Add to your Mac's `~/.reticulum/config`:

```
[[TCP to Pi]]
  type = TCPClientInterface
  target_host = 10.0.10.82
  target_port = 4242
  enabled = true
```

Then restart rnsd on the Mac or run:

```bash
rnsd
```

### Verify Mesh Connectivity

On either Mac or Pi:

```bash
rnstatus        # Show interfaces and transport info
rnpath          # Show known paths
```

### Monitor Services

```bash
# On the Pi
sudo systemctl status rnsd
sudo systemctl status meshchat
sudo systemctl status wifi-reporter

# Logs
sudo journalctl -u rnsd -f
sudo journalctl -u meshchat -f
sudo journalctl -u wifi-reporter -f
```

### MeshChat Web UI

Open in browser: `http://10.0.10.82:8080`

## WiFi Reporter

The reporter scans WiFi every 30 seconds and POSTs to the MeshVision backend:

- **Endpoint:** `POST /api/pi-report`
- **Data:** AP list (BSSID, SSID, signal, channel, security), Reticulum status, Pi system info
- **Auto-discovery:** Tries gateway IP, then common pi-star addresses
- **Override:** Set `MESHVISION_BACKEND=10.0.10.X` in the service file

### Report Format

```json
{
  "timestamp": "2025-01-15T12:00:00Z",
  "node": {
    "hostname": "raspberrypi",
    "ip_address": "10.0.10.82",
    "cpu_temp": 45.2,
    "uptime": 3600
  },
  "wifi_aps": [
    {
      "bssid": "AA:BB:CC:DD:EE:FF",
      "ssid": "pi-star",
      "signal_dbm": -45,
      "frequency_mhz": 2437,
      "channel": 6,
      "security": "WPA2/WPA3"
    }
  ],
  "wifi_summary": {
    "total_aps": 12,
    "unique_ssids": 8,
    "channels_2g": [1, 6, 11],
    "channels_5g": [36, 44]
  },
  "reticulum": {
    "running": true,
    "transport_enabled": true,
    "interfaces": [...]
  }
}
```

## Troubleshooting

**WiFi scan returns empty:** The Pi needs sudo rights for WiFi scanning.
Check that the wifi-reporter service has `AmbientCapabilities=CAP_NET_ADMIN CAP_NET_RAW`.

**MeshChat won't start:** It requires rnsd to be running first. Check:
```bash
sudo systemctl status rnsd
```

**Can't reach Pi TCP interface:** Check firewall:
```bash
sudo iptables -L -n | grep 4242
```

**Reporter can't find backend:** Set the backend IP explicitly:
```bash
sudo systemctl edit wifi-reporter
# Add: Environment=MESHVISION_BACKEND=10.0.10.X
sudo systemctl restart wifi-reporter
```
