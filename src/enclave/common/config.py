"""Configuration loader for Enclave.

Loads YAML config with sensible defaults. Config file is optional —
the system works with defaults for single-user local setups.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


DEFAULT_CONFIG_PATHS = [
    Path("/etc/enclave/enclave.yaml"),
    Path.home() / ".config" / "enclave" / "enclave.yaml",
    Path("enclave.yaml"),
]


# Prefix for synthetic (non-Matrix) room ids used when Matrix is disabled.
# Deliberately NOT a Matrix "!room:server" look-alike, so if such an id ever
# reaches the nio layer it fails loudly instead of silently misbehaving.
SYNTHETIC_ROOM_PREFIX = "local:"


def make_synthetic_room_id() -> str:
    """Return a fresh synthetic room id for a web-UI-only (Matrix-off) session."""
    import uuid
    return f"{SYNTHETIC_ROOM_PREFIX}{uuid.uuid4()}"


def is_synthetic_room(room_id: str | None) -> bool:
    """True if *room_id* is a synthetic (non-Matrix) id, never a real room."""
    return bool(room_id) and room_id.startswith(SYNTHETIC_ROOM_PREFIX)


# Sentinel marking MatrixConfig.enabled as "not explicitly set", so it can be
# derived from credential presence in __post_init__. A real bool (from yaml or
# the ENCLAVE_MATRIX_ENABLED env override) always overrides the derivation.
_MATRIX_ENABLED_UNSET: Any = object()


@dataclass
class MatrixConfig:
    """Matrix connection settings."""

    homeserver: str = ""
    user_id: str = ""
    password: str = ""
    device_name: str = "Enclave Bot"
    store_path: str = str(Path.home() / ".local" / "share" / "enclave" / "matrix_store")
    control_room_id: str = ""
    control_room_name: str = "Enclave Control"
    space_id: str = ""
    # Whether Matrix is used at all. When False, Enclave runs web-UI-only:
    # no login, no control room, no sync loop, and self.matrix is a no-op
    # NullMatrixClient. Left as the _UNSET sentinel it is derived in
    # __post_init__ from whether credentials are present (creds → True,
    # no creds → False); an explicit bool (yaml/env) always wins.
    enabled: "bool | object" = _MATRIX_ENABLED_UNSET

    def __post_init__(self) -> None:
        if self.enabled is _MATRIX_ENABLED_UNSET:
            self.enabled = self.has_credentials()

    def has_credentials(self) -> bool:
        """True if the minimum Matrix login credentials are all present."""
        return bool(self.homeserver and self.user_id and self.password)


@dataclass
class ContainerProfile:
    """A named container profile defining image and runtime options."""

    image: str = "enclave-agent:latest"
    nix_store: bool = True
    host_mounts: bool = False
    gui: bool = False
    yolo: bool = False
    fuse: bool = False  # expose /dev/fuse + SYS_ADMIN for user-space mounts
    smartcard: bool = False  # bind-mount the host pcscd socket for PC/SC card access
    persist_home: bool = False  # bind-mount <workspace>/.home over $HOME so caches persist
    host_wayland: bool = True  # with gui: mount the host Wayland socket (off = GPU/KVM only, run nested)
    auto_fusion: bool = False  # enable Auto Fusion (self-grade complexity + escalate to fusion)
    description: str = ""


@dataclass
class ContainerConfig:
    """Podman container settings."""

    image: str = "enclave-agent:latest"
    runtime: str = "podman"
    network: str = "none"
    copilot_network: str = "slirp4netns"
    dns: str = ""
    userns: str = "keep-id"
    workspace_base: str = str(Path.home() / ".local" / "share" / "enclave" / "workspaces")
    session_base: str = str(Path.home() / ".local" / "share" / "enclave" / "sessions")
    socket_dir: str = str(Path.home() / ".local" / "share" / "enclave" / "sockets")
    nix_store: str = str(Path.home() / ".local" / "share" / "enclave" / "nix")
    github_token: str = ""
    kagi_token: str = ""
    # Port mapping settings
    public_hostname: str = ""  # hostname reported to users (e.g. "dev.local"); auto-detected if empty
    port_range_start: int = 9000
    port_range_end: int = 9200
    port_bind_address: str = "127.0.0.1"  # bind address for published ports
    port_network: str = "slirp4netns"  # network mode when ports are mapped
    # Named container profiles (e.g., "dev", "light", "host")
    profiles: dict[str, ContainerProfile] = field(default_factory=lambda: {
        "dev": ContainerProfile(
            image="enclave-agent:latest",
            nix_store=True,
            host_mounts=False,
            fuse=True,
            persist_home=True,
            description="🛠️ Native App Development",
        ),
        "light": ContainerProfile(
            image="enclave-light:latest",
            nix_store=False,
            host_mounts=False,
            persist_home=True,
            description="💬 General",
        ),
        "host": ContainerProfile(
            image="",
            nix_store=False,
            host_mounts=False,
            description="🖥️ Host (no container)",
        ),
        "smartcard": ContainerProfile(
            image="enclave-agent:latest",
            nix_store=True,
            host_mounts=False,
            fuse=True,
            smartcard=True,
            persist_home=True,
            description="💳 Smartcard (PC/SC reader access)",
        ),
        "dev-nested": ContainerProfile(
            image="enclave-agent:latest",
            nix_store=True,
            host_mounts=False,
            gui=True,
            fuse=True,
            persist_home=True,
            host_wayland=False,
            description="🖥️ Dev (nested display: GPU/KVM, no host Wayland)",
        ),
    })
    default_profile: str = "dev"
    # Read-only host paths to bind-mount into containers.
    # Mapped as /host/<path> inside the container (e.g., /usr → /host/usr).
    host_mounts: list[str] = field(default_factory=lambda: [
        "/usr/bin",
        "/usr/lib",
        "/usr/libexec",
        "/usr/include",
        "/usr/share",
        "/usr/games",
        "/usr/local/bin",
        "/usr/local/lib",
        "/usr/local/include",
    ])

    def get_profile(self, name: str | None = None) -> ContainerProfile:
        """Get a container profile by name, falling back to default."""
        profile_name = name or self.default_profile
        return self.profiles.get(profile_name, self.profiles[self.default_profile])

    def profile_names(self) -> list[str]:
        """Return list of available profile names."""
        return list(self.profiles.keys())

    def get_public_hostname(self) -> str:
        """Return the public hostname for port mappings."""
        if self.public_hostname:
            return self.public_hostname
        import socket
        return socket.gethostname()


@dataclass
class UserMapping:
    """Maps a Matrix user to a Linux user."""

    matrix_id: str
    linux_user: str
    display_name: str = ""
    pronouns: str = ""
    max_sessions: int = 5
    allowed_rooms: list[str] = field(default_factory=lambda: ["*"])


@dataclass
class MemoryConfig:
    """Cross-session memory settings."""

    auto_memory: bool = False       # Enable memory persistence
    auto_dreaming: bool = False     # Enable automatic context scanning
    key_memory_limit: int = 200     # Max lines of key memories in system prompt


@dataclass
class MimirConfig:
    """Mimir memory backend settings (durable canonical-log memory).

    Independent of MemoryConfig (legacy SQLite store). Mimir augments
    rather than replaces the legacy store for now: both can coexist,
    the system prompt directs the agent which to use when. Disabled
    by default until proven stable in production.
    """

    enabled: bool = False
    # Per-agent workspace root on host. Each agent gets <root>/<agent_name>/
    # which contains canonical.log + drafts/. Mounted into the container at
    # the same path layout (under the runtime user's home).
    workspace_root: str = str(
        Path.home() / ".local" / "share" / "enclave" / "mimir"
    )
    agent_name: str = "brook"
    # Binary paths (in-container). The Containerfile installs prebuilt
    # binaries at /usr/local/bin so the defaults work for the standard image.
    mcp_bin: str = "/usr/local/bin/mimir-mcp"
    cli_bin: str = "/usr/local/bin/mimir-cli"
    librarian_bin: str = "/usr/local/bin/mimir-librarian"
    # Host-side librarian binary, used by the orchestrator's librarian
    # worker to drain pending drafts. Differs from `librarian_bin` because
    # the orchestrator runs outside the container and needs a binary that
    # links against the host's libc.
    host_librarian_bin: str = str(
        Path.home() / "Projects" / "Mimir-vendor" / "Mimir"
        / "target" / "release" / "mimir-librarian"
    )


@dataclass
class ConciergeConfig:
    """Configuration for the always-on concierge agent."""

    enabled: bool = True
    profile: str = ""  # container profile (empty = container.default_profile)


@dataclass
class HostApprovalConfig:
    """Controls the host-mode restricted-operation approval gate.

    Host-profile sessions run with the operator's full user privileges (no
    container sandbox), so restricted operations (system tools, paths outside
    the scratch space) are screened by an approval gate that surfaces a card
    in the web UI. This config governs that gate.

    `gate` is the master switch. When False the gate is bypassed entirely and
    all host requests auto-approve instantly — the emergency escape hatch that
    prevents a lockout if the approval UI is ever unreachable. It can also be
    flipped at runtime with the `ENCLAVE_HOST_APPROVAL=off` env override.

    `bypass_sessions` lists session ids that always bypass the gate even when
    it is on — for trusted operator/control sessions that must never be
    blocked (e.g. the operator agent and the always-on system session).
    """

    gate: bool = True
    bypass_sessions: list[str] = field(default_factory=list)


@dataclass
class EnclaveConfig:
    """Top-level Enclave configuration."""

    matrix: MatrixConfig = field(default_factory=MatrixConfig)
    container: ContainerConfig = field(default_factory=ContainerConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    mimir: MimirConfig = field(default_factory=MimirConfig)
    concierge: "ConciergeConfig" = field(default_factory=lambda: ConciergeConfig())
    host_approval: HostApprovalConfig = field(default_factory=HostApprovalConfig)
    approval_timeout: float = 300.0  # 5 minute approval timeout
    users: list[UserMapping] = field(default_factory=list)
    log_level: str = "INFO"
    data_dir: str = str(Path.home() / ".local" / "share" / "enclave")
    idle_timeout: int = 7200  # seconds (2h) — stop idle sessions

    def get_user_mapping(self, matrix_id: str) -> UserMapping | None:
        """Look up the Linux user mapping for a Matrix user."""
        for user in self.users:
            if user.matrix_id == matrix_id:
                return user
        return None


def _coerce_bool(value: str) -> bool:
    """Parse env-var booleans permissively."""
    return value.strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _apply_env_overrides(config: EnclaveConfig) -> None:
    """Override config values with environment variables."""
    env_map = {
        "ENCLAVE_MATRIX_HOMESERVER": ("matrix", "homeserver"),
        "ENCLAVE_MATRIX_USER": ("matrix", "user_id"),
        "ENCLAVE_MATRIX_PASSWORD": ("matrix", "password"),
        "ENCLAVE_MATRIX_DEVICE_NAME": ("matrix", "device_name"),
        "ENCLAVE_MATRIX_STORE_PATH": ("matrix", "store_path"),
        "ENCLAVE_MATRIX_CONTROL_ROOM": ("matrix", "control_room_id"),
        "ENCLAVE_MATRIX_SPACE": ("matrix", "space_id"),
        "ENCLAVE_CONTAINER_IMAGE": ("container", "image"),
        "ENCLAVE_CONTAINER_SOCKET_DIR": ("container", "socket_dir"),
        "ENCLAVE_GITHUB_TOKEN": ("container", "github_token"),
        "ENCLAVE_KAGI_TOKEN": ("container", "kagi_token"),
        "ENCLAVE_LOG_LEVEL": ("log_level",),
        "ENCLAVE_DATA_DIR": ("data_dir",),
        "ENCLAVE_MIMIR_WORKSPACE_ROOT": ("mimir", "workspace_root"),
        "ENCLAVE_MIMIR_AGENT_NAME": ("mimir", "agent_name"),
        "ENCLAVE_MIMIR_MCP_BIN": ("mimir", "mcp_bin"),
        "ENCLAVE_MIMIR_CLI_BIN": ("mimir", "cli_bin"),
        "ENCLAVE_MIMIR_LIBRARIAN_BIN": ("mimir", "librarian_bin"),
    }
    for env_var, path in env_map.items():
        value = os.environ.get(env_var)
        if value is not None:
            obj = config
            for part in path[:-1]:
                obj = getattr(obj, part)
            setattr(obj, path[-1], value)

    # Boolean env overrides (parsed permissively).
    enabled = os.environ.get("ENCLAVE_MIMIR_ENABLED")
    if enabled is not None:
        config.mimir.enabled = _coerce_bool(enabled)

    # Emergency escape hatch: ENCLAVE_HOST_APPROVAL=off disables the host
    # approval gate globally (allow-all) without editing the config file, so
    # we can recover instantly if the approval UI is ever unreachable.
    host_gate = os.environ.get("ENCLAVE_HOST_APPROVAL")
    if host_gate is not None:
        config.host_approval.gate = _coerce_bool(host_gate)

    # Matrix on/off override — parsed as a real boolean (avoids the
    # bool("false") is True trap). Wins over yaml and credential derivation.
    matrix_enabled = os.environ.get("ENCLAVE_MATRIX_ENABLED")
    if matrix_enabled is not None:
        config.matrix.enabled = _coerce_bool(matrix_enabled)


def _parse_user_mapping(data: dict[str, Any]) -> UserMapping:
    """Parse a single user mapping from config dict."""
    return UserMapping(
        matrix_id=data["matrix_id"],
        linux_user=data["linux_user"],
        display_name=data.get("display_name", ""),
        pronouns=data.get("pronouns", ""),
        max_sessions=data.get("max_sessions", 5),
        allowed_rooms=data.get("allowed_rooms", ["*"]),
    )


def load_config(path: Path | str | None = None) -> EnclaveConfig:
    """Load configuration from YAML file with env overrides.

    Search order:
    1. Explicit path argument
    2. ENCLAVE_CONFIG environment variable
    3. Default paths (see DEFAULT_CONFIG_PATHS)
    4. Fall back to all defaults
    """
    config_path: Path | None = None

    if path is not None:
        config_path = Path(path)
    elif "ENCLAVE_CONFIG" in os.environ:
        config_path = Path(os.environ["ENCLAVE_CONFIG"])
    else:
        for default in DEFAULT_CONFIG_PATHS:
            if default.exists():
                config_path = default
                break

    config = EnclaveConfig()

    if config_path is not None and config_path.exists():
        with open(config_path) as f:
            data = yaml.safe_load(f) or {}

        if "matrix" in data:
            m = data["matrix"]
            config.matrix = MatrixConfig(
                homeserver=m.get("homeserver", ""),
                user_id=m.get("user_id", ""),
                password=m.get("password", ""),
                device_name=m.get("device_name", "Enclave Bot"),
                store_path=m.get("store_path", config.matrix.store_path),
                control_room_id=m.get("control_room_id", ""),
                control_room_name=m.get("control_room_name", "Enclave Control"),
                space_id=m.get("space_id", ""),
                # Resolve enabled: explicit yaml value wins; otherwise derive
                # from whether credentials are present (see _resolve_matrix_enabled).
                enabled=m.get("enabled", _MATRIX_ENABLED_UNSET),
            )

        if "container" in data:
            c = data["container"]

            # Parse profiles if present
            profiles: dict[str, ContainerProfile] = {}
            if "profiles" in c:
                for pname, pdata in c["profiles"].items():
                    profiles[pname] = ContainerProfile(
                        image=pdata.get("image", "enclave-agent:latest"),
                        nix_store=pdata.get("nix_store", True),
                        host_mounts=pdata.get("host_mounts", True),
                        gui=pdata.get("gui", False),
                        yolo=pdata.get("yolo", False),
                        fuse=pdata.get("fuse", False),
                        smartcard=pdata.get("smartcard", False),
                        persist_home=pdata.get("persist_home", False),
                        host_wayland=pdata.get("host_wayland", True),
                        auto_fusion=pdata.get("auto_fusion", False),
                        description=pdata.get("description", ""),
                    )

            config.container = ContainerConfig(
                image=c.get("image", config.container.image),
                runtime=c.get("runtime", config.container.runtime),
                network=c.get("network", config.container.network),
                copilot_network=c.get("copilot_network", config.container.copilot_network),
                dns=c.get("dns", config.container.dns),
                userns=c.get("userns", config.container.userns),
                workspace_base=c.get("workspace_base", config.container.workspace_base),
                session_base=c.get("session_base", config.container.session_base),
                socket_dir=c.get("socket_dir", config.container.socket_dir),
                nix_store=c.get("nix_store", config.container.nix_store),
                github_token=c.get("github_token", ""),
                kagi_token=c.get("kagi_token", ""),
                default_profile=c.get("default_profile", config.container.default_profile),
                public_hostname=c.get("public_hostname", config.container.public_hostname),
                port_range_start=c.get("port_range_start", config.container.port_range_start),
                port_range_end=c.get("port_range_end", config.container.port_range_end),
                port_bind_address=c.get("port_bind_address", config.container.port_bind_address),
                port_network=c.get("port_network", config.container.port_network),
                **({"profiles": profiles} if profiles else {}),
            )

        if "users" in data:
            config.users = [_parse_user_mapping(u) for u in data["users"]]

        if "memory" in data:
            mem = data["memory"]
            config.memory = MemoryConfig(
                auto_memory=mem.get("auto_memory", config.memory.auto_memory),
                auto_dreaming=mem.get("auto_dreaming", config.memory.auto_dreaming),
                key_memory_limit=mem.get("key_memory_limit", config.memory.key_memory_limit),
            )

        if "mimir" in data:
            m = data["mimir"]
            config.mimir = MimirConfig(
                enabled=m.get("enabled", config.mimir.enabled),
                workspace_root=m.get("workspace_root", config.mimir.workspace_root),
                agent_name=m.get("agent_name", config.mimir.agent_name),
                mcp_bin=m.get("mcp_bin", config.mimir.mcp_bin),
                cli_bin=m.get("cli_bin", config.mimir.cli_bin),
                librarian_bin=m.get("librarian_bin", config.mimir.librarian_bin),
                host_librarian_bin=m.get(
                    "host_librarian_bin", config.mimir.host_librarian_bin,
                ),
            )

        config.log_level = data.get("log_level", config.log_level)
        config.data_dir = data.get("data_dir", config.data_dir)
        config.idle_timeout = data.get("idle_timeout", config.idle_timeout)
        config.approval_timeout = data.get("approval_timeout", config.approval_timeout)

        if "concierge" in data:
            cc = data["concierge"] or {}
            config.concierge = ConciergeConfig(
                enabled=cc.get("enabled", config.concierge.enabled),
                profile=cc.get("profile", config.concierge.profile),
            )

        if "host_approval" in data:
            ha = data["host_approval"] or {}
            config.host_approval = HostApprovalConfig(
                gate=ha.get("gate", config.host_approval.gate),
                bypass_sessions=ha.get(
                    "bypass_sessions", config.host_approval.bypass_sessions,
                ),
            )

    _apply_env_overrides(config)
    return config
