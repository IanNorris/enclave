"""Shared test fixtures for Enclave tests."""

import os
import tempfile
from pathlib import Path

import pytest

from enclave.common.config import EnclaveConfig, MatrixConfig, ContainerConfig


@pytest.fixture
def tmp_dir(tmp_path: Path) -> Path:
    """Provide a clean temporary directory."""
    return tmp_path


@pytest.fixture
def sample_config(tmp_path: Path) -> EnclaveConfig:
    """Provide a test configuration with temp paths."""
    return EnclaveConfig(
        matrix=MatrixConfig(
            homeserver="https://matrix.example.com",
            user_id="@bot:example.com",
            password="test-password",
            store_path=str(tmp_path / "matrix_store"),
        ),
        container=ContainerConfig(
            workspace_base=str(tmp_path / "workspaces"),
            session_base=str(tmp_path / "sessions"),
        ),
        log_level="DEBUG",
        data_dir=str(tmp_path / "data"),
    )


@pytest.fixture
def config_yaml(tmp_path: Path) -> Path:
    """Create a sample YAML config file and return its path."""
    config_file = tmp_path / "enclave.yaml"
    config_file.write_text("""
matrix:
  homeserver: "https://matrix.test.com"
  user_id: "@testbot:test.com"
  password: "secret123"
  device_name: "Test Device"

container:
  image: "test-agent:latest"
  network: "none"

users:
  - matrix_id: "@alice:test.com"
    linux_user: alice
    max_sessions: 3
  - matrix_id: "@bob:test.com"
    linux_user: bob
    max_sessions: 2

log_level: "DEBUG"
""")
    return config_file
