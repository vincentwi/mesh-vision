#!/usr/bin/env bash
# MeshVision Pi Deployment Script
# Run FROM your Mac to deploy everything to the Raspberry Pi
#
# Usage:
#   ./deploy.sh pi@10.0.10.82
#   ./deploy.sh pi@10.0.10.82 mypassword
#
# If sshpass is installed and password is provided, it will be used automatically.
# Otherwise you'll be prompted for the password interactively.
set -euo pipefail

BOLD='\033[1m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m'

log()  { echo -e "${GREEN}[Deploy]${NC} $*"; }
warn() { echo -e "${YELLOW}[Deploy]${NC} $*"; }
err()  { echo -e "${RED}[Deploy]${NC} $*"; exit 1; }
info() { echo -e "${CYAN}[Deploy]${NC} $*"; }

# --- Args ---
PI_TARGET="${1:-}"
PI_PASS="${2:-}"

if [[ -z "$PI_TARGET" ]]; then
    echo ""
    echo -e "${BOLD}MeshVision Pi Deployer${NC}"
    echo ""
    echo "Usage: $0 <user@host> [password]"
    echo ""
    echo "Examples:"
    echo "  $0 pi@10.0.10.82"
    echo "  $0 pi@10.0.10.82 raspberry"
    echo ""
    exit 1
fi

PI_USER="${PI_TARGET%%@*}"
PI_HOST="${PI_TARGET##*@}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REMOTE_DIR="/tmp/mesh-vision"

log "MeshVision Pi Deployer"
log "Target: ${PI_TARGET}"
log "Source: ${SCRIPT_DIR}"
echo ""

# --- Setup SSH/SCP commands ---
SSH_OPTS="-o StrictHostKeyChecking=no -o ConnectTimeout=10"

if [[ -n "$PI_PASS" ]] && command -v sshpass &>/dev/null; then
    SSH_CMD="sshpass -p '${PI_PASS}' ssh ${SSH_OPTS}"
    SCP_CMD="sshpass -p '${PI_PASS}' scp ${SSH_OPTS}"
    log "Using sshpass for authentication"
elif [[ -n "$PI_PASS" ]]; then
    warn "sshpass not installed — install with: brew install sshpass"
    warn "You'll be prompted for the password at each step"
    warn "Password provided: ${PI_PASS}"
    SSH_CMD="ssh ${SSH_OPTS}"
    SCP_CMD="scp ${SSH_OPTS}"
else
    SSH_CMD="ssh ${SSH_OPTS}"
    SCP_CMD="scp ${SSH_OPTS}"
fi

# --- Test connectivity ---
log "Testing connectivity to ${PI_HOST}..."
if ! ping -c 1 -W 3 "${PI_HOST}" &>/dev/null; then
    err "Cannot reach ${PI_HOST} — check network connection"
fi
log "  ✓ Host reachable"

# --- Test SSH ---
log "Testing SSH connection..."
if [[ -n "$PI_PASS" ]] && command -v sshpass &>/dev/null; then
    if ! sshpass -p "${PI_PASS}" ssh ${SSH_OPTS} "${PI_TARGET}" "echo ok" &>/dev/null; then
        err "SSH connection failed — check credentials"
    fi
else
    if ! ssh ${SSH_OPTS} "${PI_TARGET}" "echo ok" 2>/dev/null; then
        err "SSH connection failed — check credentials or SSH key"
    fi
fi
log "  ✓ SSH connected"

# Helper to run remote commands
remote_cmd() {
    if [[ -n "$PI_PASS" ]] && command -v sshpass &>/dev/null; then
        sshpass -p "${PI_PASS}" ssh ${SSH_OPTS} "${PI_TARGET}" "$@"
    else
        ssh ${SSH_OPTS} "${PI_TARGET}" "$@"
    fi
}

remote_scp() {
    if [[ -n "$PI_PASS" ]] && command -v sshpass &>/dev/null; then
        sshpass -p "${PI_PASS}" scp ${SSH_OPTS} "$@"
    else
        scp ${SSH_OPTS} "$@"
    fi
}

# --- Upload files ---
log "Creating remote directory..."
remote_cmd "rm -rf ${REMOTE_DIR} && mkdir -p ${REMOTE_DIR}"

log "Uploading deployment files..."
remote_scp "${SCRIPT_DIR}/install.sh" "${PI_TARGET}:${REMOTE_DIR}/"
remote_scp "${SCRIPT_DIR}/reticulum-config" "${PI_TARGET}:${REMOTE_DIR}/"
remote_scp "${SCRIPT_DIR}/rnsd.service" "${PI_TARGET}:${REMOTE_DIR}/"
remote_scp "${SCRIPT_DIR}/meshchat.service" "${PI_TARGET}:${REMOTE_DIR}/"
remote_scp "${SCRIPT_DIR}/wifi-reporter.py" "${PI_TARGET}:${REMOTE_DIR}/"
remote_scp "${SCRIPT_DIR}/wifi-reporter.service" "${PI_TARGET}:${REMOTE_DIR}/"
log "  ✓ Files uploaded to ${REMOTE_DIR}"

# --- Run installer ---
log "Running installer on Pi (this may take a few minutes)..."
echo ""
remote_cmd "sudo bash ${REMOTE_DIR}/install.sh" 2>&1 | while IFS= read -r line; do
    echo "  [Pi] ${line}"
done
echo ""

# --- Verify services ---
log "Verifying services..."
echo ""

for svc in rnsd meshchat wifi-reporter; do
    STATUS=$(remote_cmd "systemctl is-active ${svc}.service 2>/dev/null || echo 'inactive'")
    if [[ "$STATUS" == "active" ]]; then
        info "  ✓ ${svc}.service is ${GREEN}active${NC}"
    else
        warn "  ✗ ${svc}.service is ${RED}${STATUS}${NC}"
    fi
done

echo ""

# --- Get Pi info ---
PI_IP=$(remote_cmd "hostname -I | awk '{print \$1}'")

log "=========================================="
log " Deployment Complete!"
log "=========================================="
log ""
log " Pi Address:        ${PI_IP}"
log " Reticulum TCP:     ${PI_IP}:4242"
log " MeshChat Web UI:   http://${PI_IP}:8080"
log ""
log " To add this Pi to your Mac's Reticulum:"
log "   Edit ~/.reticulum/config and add:"
log ""
log "   [[TCP to Pi]]"
log "     type = TCPClientInterface"
log "     target_host = ${PI_IP}"
log "     target_port = 4242"
log "     enabled = true"
log ""
log " Monitor Pi logs:"
log "   ssh ${PI_TARGET} 'sudo journalctl -u rnsd -f'"
log "   ssh ${PI_TARGET} 'sudo journalctl -u wifi-reporter -f'"
log ""
