#!/usr/bin/env bash
# install.sh — Install Enclave components on the host system.
#
# Usage:
#   ./install.sh           # Install everything
#   ./install.sh orchestrator  # Install orchestrator only
#   ./install.sh broker    # Install priv broker only
#   ./install.sh image     # Build container image only

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
YELLOW='\033[1;33m'
GREEN='\033[1;32m'
RED='\033[1;31m'
NC='\033[0m'

info()  { echo -e "${GREEN}[+]${NC} $*"; }
warn()  { echo -e "${YELLOW}[!]${NC} $*"; }
error() { echo -e "${RED}[!]${NC} $*" >&2; }

# ── Orchestrator (user-level install) ──────────────────────────

install_orchestrator() {
    info "Installing Enclave orchestrator..."

    # Install Python package in editable mode
    pip install --user -e "$SCRIPT_DIR" 2>/dev/null || \
        pip install -e "$SCRIPT_DIR"
    info "Python package installed (entry point: enclave)"

    # Create config directory
    local config_dir="$HOME/.config/enclave"
    mkdir -p "$config_dir"
    if [ ! -f "$config_dir/enclave.yaml" ]; then
        cp "$SCRIPT_DIR/enclave.yaml.example" "$config_dir/enclave.yaml"
        warn "Config created at $config_dir/enclave.yaml — edit before starting!"
    else
        info "Config already exists at $config_dir/enclave.yaml"
    fi

    # Install systemd user service
    local service_dir="$HOME/.config/systemd/user"
    mkdir -p "$service_dir"
    cp "$SCRIPT_DIR/systemd/enclave.service" "$service_dir/"
    systemctl --user daemon-reload
    info "Systemd user service installed"

    # Enable lingering so service survives logout
    if ! loginctl show-user "$USER" 2>/dev/null | grep -q "Linger=yes"; then
        warn "Enabling loginctl linger for $USER (needs sudo)..."
        sudo loginctl enable-linger "$USER" || \
            warn "Could not enable linger — run: sudo loginctl enable-linger $USER"
    fi

    info "Orchestrator installed! Start with: systemctl --user enable --now enclave"
}

# ── Privilege Broker (system-level install, requires root) ─────

install_broker() {
    info "Installing Enclave privilege broker..."

    if [ "$EUID" -ne 0 ] && ! command -v sudo &>/dev/null; then
        error "Privilege broker requires root. Run with sudo or as root."
        return 1
    fi

    local SUDO=""
    [ "$EUID" -ne 0 ] && SUDO="sudo"

    # Build the broker
    if command -v cargo &>/dev/null; then
        info "Building priv-broker with cargo..."
        (cd "$SCRIPT_DIR/priv-broker" && cargo build --release)
        $SUDO cp "$SCRIPT_DIR/priv-broker/target/release/enclave-priv-broker" /usr/local/bin/
        $SUDO chmod 755 /usr/local/bin/enclave-priv-broker
        info "Binary installed to /usr/local/bin/enclave-priv-broker"
    else
        error "cargo not found — install Rust toolchain first"
        return 1
    fi

    # Install config
    $SUDO mkdir -p /etc/enclave
    if [ ! -f /etc/enclave/priv-broker.toml ]; then
        $SUDO cp "$SCRIPT_DIR/priv-broker/config/priv-broker.toml" /etc/enclave/
        warn "Config created at /etc/enclave/priv-broker.toml — edit before starting!"
    fi

    # Install systemd service
    $SUDO cp "$SCRIPT_DIR/priv-broker/config/enclave-priv-broker.service" \
        /etc/systemd/system/
    $SUDO systemctl daemon-reload
    info "Systemd system service installed"

    info "Broker installed! Start with: sudo systemctl enable --now enclave-priv-broker"
}

# ── Container Image ────────────────────────────────────────────

build_image() {
    info "Building Enclave agent container image..."

    local runtime="podman"
    command -v podman &>/dev/null || runtime="docker"

    $runtime build -t enclave-agent:latest -f "$SCRIPT_DIR/container/Containerfile" "$SCRIPT_DIR"
    info "Container image built: enclave-agent:latest"
}

# ── Main ───────────────────────────────────────────────────────

case "${1:-all}" in
    orchestrator) install_orchestrator ;;
    broker)       install_broker ;;
    image)        build_image ;;
    all)
        install_orchestrator
        build_image
        install_broker 2>/dev/null || warn "Skipped broker (needs root). Run: sudo $0 broker"
        echo
        info "Installation complete!"
        info "  1. Edit ~/.config/enclave/enclave.yaml"
        info "  2. systemctl --user enable --now enclave"
        info "  3. sudo systemctl enable --now enclave-priv-broker"
        ;;
    *)
        echo "Usage: $0 [orchestrator|broker|image|all]"
        exit 1
        ;;
esac
