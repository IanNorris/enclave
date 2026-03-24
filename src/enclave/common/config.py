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


@dataclass
class MatrixConfig:
    """Matrix connection settings."""

    homeserver: str = ""
    user_id: str = ""
    password: str = ""
    device_name: str = "Enclave Bot"
    store_path: str = str(Path.home() / ".local" / "share" / "enclave" / "matrix_store")
    control_room_id: str = ""
    space_id: str = ""


@dataclass
class ContainerConfig:
    """Podman container settings."""

    image: str = "enclave-agent:latest"
    runtime: str = "podman"
    network: str = "none"
    userns: str = "keep-id"
    workspace_base: str = str(Path.home() / ".local" / "share" / "enclave" / "workspaces")
    session_base: str = str(Path.home() / ".local" / "share" / "enclave" / "sessions")
    socket_dir: str = str(Path.home() / ".local" / "share" / "enclave" / "sockets")


@dataclass
class UserMapping:
    """Maps a Matrix user to a Linux user."""

    matrix_id: str
    linux_user: str
    max_sessions: int = 5
    can_approve_privilege: bool = True
    allowed_rooms: list[str] = field(default_factory=lambda: ["*"])


@dataclass
class PrivBrokerConfig:
    """Privilege broker connection settings."""

    socket_path: str = "/run/enclave-priv/broker.sock"
    timeout: float = 300.0  # 5 minute approval timeout


@dataclass
class EnclaveConfig:
    """Top-level Enclave configuration."""

    matrix: MatrixConfig = field(default_factory=MatrixConfig)
    container: ContainerConfig = field(default_factory=ContainerConfig)
    priv_broker: PrivBrokerConfig = field(default_factory=PrivBrokerConfig)
    users: list[UserMapping] = field(default_factory=list)
    log_level: str = "INFO"
    data_dir: str = str(Path.home() / ".local" / "share" / "enclave")

    def get_user_mapping(self, matrix_id: str) -> UserMapping | None:
        """Look up the Linux user mapping for a Matrix user."""
        for user in self.users:
            if user.matrix_id == matrix_id:
                return user
        return None


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
        "ENCLAVE_LOG_LEVEL": ("log_level",),
        "ENCLAVE_DATA_DIR": ("data_dir",),
    }
    for env_var, path in env_map.items():
        value = os.environ.get(env_var)
        if value is not None:
            obj = config
            for part in path[:-1]:
                obj = getattr(obj, part)
            setattr(obj, path[-1], value)


def _parse_user_mapping(data: dict[str, Any]) -> UserMapping:
    """Parse a single user mapping from config dict."""
    return UserMapping(
        matrix_id=data["matrix_id"],
        linux_user=data["linux_user"],
        max_sessions=data.get("max_sessions", 5),
        can_approve_privilege=data.get("can_approve_privilege", True),
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
                space_id=m.get("space_id", ""),
            )

        if "container" in data:
            c = data["container"]
            config.container = ContainerConfig(
                image=c.get("image", config.container.image),
                runtime=c.get("runtime", config.container.runtime),
                network=c.get("network", config.container.network),
                userns=c.get("userns", config.container.userns),
                workspace_base=c.get("workspace_base", config.container.workspace_base),
                session_base=c.get("session_base", config.container.session_base),
                socket_dir=c.get("socket_dir", config.container.socket_dir),
            )

        if "priv_broker" in data:
            p = data["priv_broker"]
            config.priv_broker = PrivBrokerConfig(
                socket_path=p.get("socket_path", config.priv_broker.socket_path),
                timeout=p.get("timeout", config.priv_broker.timeout),
            )

        if "users" in data:
            config.users = [_parse_user_mapping(u) for u in data["users"]]

        config.log_level = data.get("log_level", config.log_level)
        config.data_dir = data.get("data_dir", config.data_dir)

    _apply_env_overrides(config)
    return config
