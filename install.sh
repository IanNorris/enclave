#!/usr/bin/env bash
# install.sh — Install Enclave components on the host system.
#
# Usage:
#   ./install.sh           # Install everything
#   ./install.sh orchestrator  # Install orchestrator only
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
    image)        build_image ;;
    all)
        install_orchestrator
        build_image
        echo
        info "Installation complete!"
        info "  1. Edit ~/.config/enclave/enclave.yaml"
        info "  2. systemctl --user enable --now enclave"
        ;;
    *)
        echo "Usage: $0 [orchestrator|image|all]"
        exit 1
        ;;
esac
