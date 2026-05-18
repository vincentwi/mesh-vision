#!/usr/bin/env bash
# MeshVision Raspberry Pi Install Script
# Installs Reticulum, MeshChat, NomadNet, and WiFi reporter
set -euo pipefail

BOLD='\033[1m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

log()  { echo -e "${GREEN}[MeshVision]${NC} $*"; }
warn() { echo -e "${YELLOW}[WARNING]${NC} $*"; }
err()  { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

# --- Detect user and home ---
MESH_USER="${SUDO_USER:-$(whoami)}"
MESH_HOME=$(eval echo "~${MESH_USER}")
RNS_DIR="${MESH_HOME}/.reticulum"
INSTALL_SRC="$(cd "$(dirname "$0")" && pwd)"

log "MeshVision Pi Node Installer"
log "User: ${MESH_USER} | Home: ${MESH_HOME}"
log "Source: ${INSTALL_SRC}"

# --- Must run as root ---
if [[ $EUID -ne 0 ]]; then
    err "This script must be run with sudo: sudo bash install.sh"
fi

# --- System packages ---
log "Updating apt and installing system dependencies..."
apt-get update -qq
apt-get install -y -qq python3 python3-pip python3-venv \
    wireless-tools iw net-tools curl jq

# --- Python packages ---
log "Installing Reticulum ecosystem via pip..."
pip3 install --break-system-packages --upgrade \
    rns nomadnet lxmf reticulummeshchat 2>/dev/null || \
pip3 install --upgrade rns nomadnet lxmf reticulummeshchat

# Verify installs
for cmd in rnsd rnstatus nomadnet meshchat; do
    if command -v "$cmd" &>/dev/null; then
        log "  ✓ $cmd installed: $(command -v $cmd)"
    else
        warn "  ✗ $cmd not found in PATH (may still work via python -m)"
    fi
done

# --- Reticulum config ---
log "Setting up Reticulum config..."
mkdir -p "${RNS_DIR}"

if [[ -f "${RNS_DIR}/config" ]]; then
    cp "${RNS_DIR}/config" "${RNS_DIR}/config.backup.$(date +%s)"
    log "  Backed up existing config"
fi

cp "${INSTALL_SRC}/reticulum-config" "${RNS_DIR}/config"
chown -R "${MESH_USER}:${MESH_USER}" "${RNS_DIR}"
log "  ✓ Reticulum config installed (transport=True, TCP:4242)"

# --- WiFi reporter ---
log "Installing WiFi reporter..."
mkdir -p /opt/meshvision
cp "${INSTALL_SRC}/wifi-reporter.py" /opt/meshvision/wifi-reporter.py
chmod +x /opt/meshvision/wifi-reporter.py
chown -R "${MESH_USER}:${MESH_USER}" /opt/meshvision

# --- Systemd services ---
log "Installing systemd services..."

cp "${INSTALL_SRC}/rnsd.service" /etc/systemd/system/rnsd.service
cp "${INSTALL_SRC}/meshchat.service" /etc/systemd/system/meshchat.service
cp "${INSTALL_SRC}/wifi-reporter.service" /etc/systemd/system/wifi-reporter.service

# Patch User= in service files to match actual user
sed -i "s/User=pi/User=${MESH_USER}/g" /etc/systemd/system/rnsd.service
sed -i "s/User=pi/User=${MESH_USER}/g" /etc/systemd/system/meshchat.service
sed -i "s/User=pi/User=${MESH_USER}/g" /etc/systemd/system/wifi-reporter.service

# Patch home dir references
sed -i "s|/home/pi|${MESH_HOME}|g" /etc/systemd/system/rnsd.service
sed -i "s|/home/pi|${MESH_HOME}|g" /etc/systemd/system/meshchat.service
sed -i "s|/home/pi|${MESH_HOME}|g" /etc/systemd/system/wifi-reporter.service

systemctl daemon-reload

# Enable and start services
for svc in rnsd meshchat wifi-reporter; do
    systemctl enable "${svc}.service"
    systemctl start "${svc}.service" || warn "Failed to start ${svc} (may need reboot)"
    log "  ✓ ${svc}.service enabled"
done

# --- Wait for rnsd to initialize ---
log "Waiting for rnsd to initialize..."
sleep 3

# --- Connectivity test ---
log "Testing Reticulum connectivity..."
if sudo -u "${MESH_USER}" rnstatus 2>/dev/null; then
    log "  ✓ Reticulum is running"
else
    warn "  rnstatus returned non-zero (rnsd may still be initializing)"
fi

# --- Network info ---
PI_IP=$(hostname -I | awk '{print $1}')
log ""
log "=========================================="
log " MeshVision Pi Node Install Complete!"
log "=========================================="
log ""
log " Pi IP Address:     ${PI_IP}"
log " Reticulum Config:  ${RNS_DIR}/config"
log " Transport Node:    ENABLED"
log " TCP Server:        ${PI_IP}:4242"
log ""
log " Services:"
log "   rnsd            → Reticulum daemon (transport relay)"
log "   meshchat        → MeshChat web UI on :8080"
log "   wifi-reporter   → WiFi scanner → MeshVision backend"
log ""
log " Commands:"
log "   rnstatus              - Check Reticulum status"
log "   sudo systemctl status rnsd"
log "   sudo systemctl status meshchat"
log "   sudo journalctl -u wifi-reporter -f"
log ""
log " To connect from Mac, add to Mac's Reticulum config:"
log "   [[TCP ${PI_IP}]]"
log "     type = TCPClientInterface"
log "     target_host = ${PI_IP}"
log "     target_port = 4242"
log "     enabled = true"
log ""
