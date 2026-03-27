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

    if [ "$EUID" -eq 0 ]; then
        error "The orchestrator is a user-level service — do not run with sudo."
        error "Run: ./install.sh orchestrator"
        return 1
    fi

    local venv_dir="$HOME/.local/share/enclave/venv"
    local bin_dir="$HOME/.local/bin"
    mkdir -p "$bin_dir"

    # Try standard pip install first; fall back to a venv for
    # externally-managed environments (PEP 668 — NixOS, modern Debian, etc.)
    if pip install --user -e "$SCRIPT_DIR" 2>/dev/null || \
       pip install -e "$SCRIPT_DIR" 2>/dev/null; then
        info "Python package installed (entry point: enclave)"
    else
        warn "Externally-managed Python detected — installing in a venv"
        mkdir -p "$(dirname "$venv_dir")"
        python3 -m venv --clear "$venv_dir"

        # On NixOS, native deps (libolm, gcc, pkg-config) live in the nix store.
        # Use the flake's devShell to provide them during pip install.
        if [ -f /etc/NIXOS ] && command -v nix &>/dev/null && [ -f "$SCRIPT_DIR/flake.nix" ]; then
            info "NixOS detected — using nix develop for native build deps"
            nix develop "$SCRIPT_DIR" --command "$venv_dir/bin/pip" install -e "$SCRIPT_DIR"
        else
            "$venv_dir/bin/pip" install -e "$SCRIPT_DIR"
        fi

        # On NixOS, native libs (libstdc++) are in the nix store. Create a
        # wrapper script instead of a symlink so LD_LIBRARY_PATH survives
        # Home Manager rebuilds that replace the systemd service file.
        if [ -f /etc/NIXOS ]; then
            local gcc_lib
            gcc_lib="$(find /nix/store -maxdepth 1 -name '*-gcc-*-lib' -type d 2>/dev/null | sort -V | tail -1)"
            if [ -n "$gcc_lib" ] && [ -d "$gcc_lib/lib" ]; then
                rm -f "$bin_dir/enclave"
                cat > "$bin_dir/enclave" <<WRAPPER
#!/bin/sh
export LD_LIBRARY_PATH="$gcc_lib/lib\${LD_LIBRARY_PATH:+:\$LD_LIBRARY_PATH}"
exec "$venv_dir/bin/enclave" "\$@"
WRAPPER
                chmod +x "$bin_dir/enclave"
                info "Created NixOS wrapper with LD_LIBRARY_PATH"
            else
                ln -sf "$venv_dir/bin/enclave" "$bin_dir/enclave"
            fi
        else
            ln -sf "$venv_dir/bin/enclave" "$bin_dir/enclave"
        fi
        ln -sf "$venv_dir/bin/enclavectl" "$bin_dir/enclavectl" 2>/dev/null || true
        info "Installed in venv at $venv_dir (linked to $bin_dir/)"
    fi

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
    install -m 644 "$SCRIPT_DIR/systemd/enclave.service" "$service_dir/"

    systemctl --user daemon-reload
    info "Systemd user service installed"

    # Enable lingering so service survives logout
    if ! loginctl show-user "$USER" 2>/dev/null | grep -q "Linger=yes"; then
        warn "Enabling loginctl linger for $USER (needs sudo)..."
        sudo loginctl enable-linger "$USER" || \
            warn "Could not enable linger — run: sudo loginctl enable-linger $USER"
    fi

    systemctl --user enable --now enclave
    info "Orchestrator installed and started"
}

# ── Privilege Broker (system-level install, requires root) ─────

install_broker() {
    info "Installing Enclave privilege broker..."

    # On NixOS, system services must be declared via the NixOS module
    if [ -f /etc/NIXOS ]; then
        warn "NixOS detected — use the NixOS module for the privilege broker."
        warn "Add to your NixOS configuration:"
        warn ""
        warn "  services.enclave.enable = true;"
        warn "  services.enclave.broker.allowedUser = \"$USER\";"
        warn ""
        warn "Then run: sudo nixos-rebuild switch"
        return 0
    fi

    # Build as current user (needs cargo/rustup), then install as root.
    # This avoids requiring rustup to be configured for the root user.
    if ! command -v cargo &>/dev/null; then
        error "cargo not found — install Rust toolchain first"
        return 1
    fi

    # Verify cargo is actually usable (rustup may be present without a toolchain)
    if ! cargo --version &>/dev/null; then
        error "cargo is installed but no Rust toolchain is configured."
        error "Run: rustup default stable"
        return 1
    fi

    info "Building priv-broker with cargo..."
    (cd "$SCRIPT_DIR/priv-broker" && cargo build --release)

    local binary="$SCRIPT_DIR/priv-broker/target/release/enclave-priv-broker"
    if [ ! -f "$binary" ]; then
        error "Build succeeded but binary not found at $binary"
        return 1
    fi

    # Elevate to root only for installation
    local SUDO=""
    if [ "$EUID" -ne 0 ]; then
        if ! command -v sudo &>/dev/null; then
            error "Installation requires root. Run the install step with sudo."
            return 1
        fi
        SUDO="sudo"
    fi

    local install_dir="/usr/local/bin"
    [ -d "$install_dir" ] || install_dir="/usr/bin"
    $SUDO cp "$binary" "$install_dir/enclave-priv-broker"
    $SUDO chmod 755 "$install_dir/enclave-priv-broker"
    info "Binary installed to $install_dir/enclave-priv-broker"

    # Install config
    $SUDO mkdir -p /etc/enclave
    if [ ! -f /etc/enclave/priv-broker.toml ]; then
        $SUDO cp "$SCRIPT_DIR/priv-broker/config/priv-broker.toml" /etc/enclave/
        warn "Config created at /etc/enclave/priv-broker.toml — edit before starting!"
    fi

    # Install systemd service (patch binary path to match install location)
    local service_tmp
    service_tmp="$(mktemp)"
    sed "s|ExecStart=/usr/local/bin/enclave-priv-broker|ExecStart=$install_dir/enclave-priv-broker|" \
        "$SCRIPT_DIR/priv-broker/config/enclave-priv-broker.service" > "$service_tmp"
    $SUDO cp "$service_tmp" /etc/systemd/system/enclave-priv-broker.service
    rm -f "$service_tmp"
    $SUDO systemctl daemon-reload
    $SUDO systemctl enable --now enclave-priv-broker
    info "Privilege broker installed and started"
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
