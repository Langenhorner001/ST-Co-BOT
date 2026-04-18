#!/bin/bash
# ============================================================
#   ST-CHECKER-BOT — One-Click AWS EC2 Setup Script
#   Usage: bash setup.sh
# ============================================================

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

log()  { echo -e "${CYAN}[INFO]${NC} $1"; }
ok()   { echo -e "${GREEN}[OK]${NC}   $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
fail() { echo -e "${RED}[FAIL]${NC} $1"; exit 1; }

echo -e "${BOLD}"
echo "╔══════════════════════════════════════╗"
echo "║     🤖  ST-CHECKER-BOT SETUP         ║"
echo "║     AWS EC2 One-Click Installer      ║"
echo "╚══════════════════════════════════════╝"
echo -e "${NC}"

# ── 1. System update & dependencies ──────────────────────────
log "Updating system packages..."
sudo apt-get update -qq && sudo apt-get upgrade -y -qq
ok "System updated."

log "Installing Python3, pip, git, screen..."
sudo apt-get install -y -qq python3 python3-pip python3-venv git screen curl
ok "System packages installed."

# ── 2. Bot token & admin ID ───────────────────────────────────
echo ""
echo -e "${BOLD}🔑 Bot Configuration${NC}"

if [ -z "$BOT_TOKEN" ]; then
    read -rp "Enter your BOT_TOKEN (from @BotFather): " BOT_TOKEN
fi
if [ -z "$BOT_TOKEN" ]; then
    fail "BOT_TOKEN is required. Get one from @BotFather on Telegram."
fi

if [ -z "$ADMIN_ID" ]; then
    read -rp "Enter your ADMIN_ID (your Telegram user ID): " ADMIN_ID
fi
if [ -z "$ADMIN_ID" ]; then
    fail "ADMIN_ID is required. Get it from @userinfobot on Telegram."
fi

export BOT_TOKEN="$BOT_TOKEN"
export ADMIN_ID="$ADMIN_ID"

# Persist env vars so they survive reboots
PROFILE_FILE="$HOME/.bashrc"
grep -q "BOT_TOKEN" "$PROFILE_FILE" 2>/dev/null && \
    sed -i "/BOT_TOKEN/d" "$PROFILE_FILE"
grep -q "ADMIN_ID" "$PROFILE_FILE" 2>/dev/null && \
    sed -i "/ADMIN_ID/d" "$PROFILE_FILE"

echo "export BOT_TOKEN=\"$BOT_TOKEN\"" >> "$PROFILE_FILE"
echo "export ADMIN_ID=\"$ADMIN_ID\""   >> "$PROFILE_FILE"
ok "Credentials saved to $PROFILE_FILE."

# ── 3. Project directory ──────────────────────────────────────
BOT_DIR="$HOME/st-checker-bot"

if [ -d "$BOT_DIR" ]; then
    warn "Directory $BOT_DIR already exists — skipping clone."
else
    log "Creating bot directory at $BOT_DIR..."
    mkdir -p "$BOT_DIR"
fi

log "Copying bot files..."
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cp -r "$SCRIPT_DIR"/. "$BOT_DIR/"
ok "Files copied to $BOT_DIR."

cd "$BOT_DIR"

# ── 4. Python virtual environment ────────────────────────────
log "Creating Python virtual environment..."
python3 -m venv venv
source venv/bin/activate
ok "Virtual environment activated."

# ── 5. Install Python requirements ───────────────────────────
log "Installing Python packages from requirements.txt..."
pip install --upgrade pip -q

RETRIES=3
for attempt in $(seq 1 $RETRIES); do
    if pip install -r requirements.txt -q; then
        ok "Python packages installed (attempt $attempt)."
        break
    else
        warn "Install attempt $attempt failed."
        if [ "$attempt" -eq "$RETRIES" ]; then
            fail "Failed to install dependencies after $RETRIES attempts."
        fi
        sleep 3
    fi
done

# ── 6. Init data files if missing ────────────────────────────
log "Initialising data files..."
[ -f data.json ]          || echo "{}" > data.json
[ -f user_proxies.json ]  || echo "{}" > user_proxies.json
[ -f user_amounts.json ]  || echo "{}" > user_amounts.json
ok "Data files ready."

# ── 7. Create systemd service (recommended) ──────────────────
SERVICE_NAME="st-checker-bot"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

if command -v systemctl &>/dev/null; then
    log "Creating systemd service for auto-start on reboot..."
    sudo tee "$SERVICE_FILE" > /dev/null <<EOF
[Unit]
Description=ST-CHECKER Telegram Bot
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$BOT_DIR
Environment="BOT_TOKEN=$BOT_TOKEN"
Environment="ADMIN_ID=$ADMIN_ID"
ExecStart=$BOT_DIR/venv/bin/python3 $BOT_DIR/main.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

    sudo systemctl daemon-reload
    sudo systemctl enable "$SERVICE_NAME"
    ok "Systemd service registered — will auto-start on reboot."
    USE_SYSTEMD=true
else
    warn "systemd not available — will use screen session instead."
    USE_SYSTEMD=false
fi

# ── 8. Start the bot ─────────────────────────────────────────
echo ""
log "Starting the bot..."

if [ "$USE_SYSTEMD" = true ]; then
    sudo systemctl restart "$SERVICE_NAME"
    sleep 3
    if systemctl is-active --quiet "$SERVICE_NAME"; then
        ok "Bot started via systemd. Check logs: journalctl -u $SERVICE_NAME -f"
    else
        warn "Systemd start failed — falling back to screen session."
        USE_SYSTEMD=false
    fi
fi

if [ "$USE_SYSTEMD" = false ]; then
    screen -S st-checker-bot -X quit 2>/dev/null || true
    screen -dmS st-checker-bot bash -c \
        "cd $BOT_DIR && source venv/bin/activate && BOT_TOKEN=$BOT_TOKEN ADMIN_ID=$ADMIN_ID python3 main.py 2>&1 | tee bot.log"
    sleep 3
    if screen -list | grep -q "st-checker-bot"; then
        ok "Bot started in screen session 'st-checker-bot'."
        ok "Attach with: screen -r st-checker-bot"
    else
        fail "Bot failed to start. Check bot.log for details."
    fi
fi

# ── 9. Final summary ─────────────────────────────────────────
echo ""
echo -e "${GREEN}${BOLD}"
echo "╔══════════════════════════════════════╗"
echo "║  ✅  Bot deployed successfully!      ║"
echo "╠══════════════════════════════════════╣"
echo "║  Commands added:                     ║"
echo "║    /ping   — latency check           ║"
echo "║    /status — uptime & environment    ║"
echo "║    /stats  — usage statistics        ║"
echo "╠══════════════════════════════════════╣"
echo "║  Useful commands:                    ║"
if [ "$USE_SYSTEMD" = true ]; then
echo "║  • sudo systemctl status $SERVICE_NAME"
echo "║  • journalctl -u $SERVICE_NAME -f    ║"
echo "║  • sudo systemctl restart $SERVICE_NAME"
else
echo "║  • screen -r st-checker-bot          ║"
echo "║  • tail -f $BOT_DIR/bot.log          ║"
fi
echo "╚══════════════════════════════════════╝"
echo -e "${NC}"
