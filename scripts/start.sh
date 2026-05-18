#!/usr/bin/env bash
# ============================================================
#  mesh-vision · start.sh
#  Boots the MeshVision backend + opens the HUD
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV_PYTHON="$PROJECT_DIR/.venv/bin/python3"
BACKEND_DIR="$PROJECT_DIR/backend"
SERVER_PID=""

# ── Colors ───────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; NC='\033[0m'

info()  { echo -e "${CYAN}[mesh-vision]${NC} $*"; }
ok()    { echo -e "${GREEN}[✓]${NC} $*"; }
warn()  { echo -e "${YELLOW}[!]${NC} $*"; }
err()   { echo -e "${RED}[✗]${NC} $*"; }

# ── Cleanup on exit ──────────────────────────────────────────
cleanup() {
    echo ""
    info "Shutting down..."
    if [[ -n "$SERVER_PID" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
        kill "$SERVER_PID" 2>/dev/null || true
        wait "$SERVER_PID" 2>/dev/null || true
        ok "Backend server stopped"
    fi
    # Kill any rnsd subprocesses
    pkill -f "rnsd --config.*mesh_configs" 2>/dev/null || true
    info "Goodbye."
    exit 0
}
trap cleanup SIGINT SIGTERM EXIT

# ── Check venv ────────────────────────────────────────────────
if [[ ! -x "$VENV_PYTHON" ]]; then
    err "Virtual environment not found at $PROJECT_DIR/.venv"
    err "Run:  cd $PROJECT_DIR && python3 -m venv .venv && .venv/bin/pip install rns lxmf fastapi 'uvicorn[standard]' bleak websockets opencv-python-headless numpy pyobjc-core pyobjc-framework-CoreWLAN"
    exit 1
fi

ok "Python: $($VENV_PYTHON --version)"

# ── Start FastAPI backend ────────────────────────────────────
info "Starting MeshVision backend on http://localhost:8420 ..."
cd "$BACKEND_DIR"

"$VENV_PYTHON" -m uvicorn server:app \
    --host 0.0.0.0 \
    --port 8420 \
    --log-level info &
SERVER_PID=$!

sleep 3

if kill -0 "$SERVER_PID" 2>/dev/null; then
    ok "Backend running (PID $SERVER_PID)"
else
    err "Backend failed to start"
    exit 1
fi

# ── Open browser ─────────────────────────────────────────────
URL="http://localhost:8420"
info "Opening $URL ..."
open "$URL" 2>/dev/null || true

# ── Hold ─────────────────────────────────────────────────────
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo -e "  ${GREEN}◈ MESH VISION running${NC}"
echo -e "  HUD:     ${CYAN}$URL${NC}"
echo -e "  API:     ${CYAN}$URL/api/status${NC}"
echo -e "  Send:    ${CYAN}curl -X POST $URL/api/send-message -H 'Content-Type: application/json' -d '{\"content\": \"hello\"}'${NC}"
echo -e "  Press ${YELLOW}Ctrl+C${NC} to stop"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

wait "$SERVER_PID"
