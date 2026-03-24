"""Tests for configuration loading."""

import os
from pathlib import Path

import pytest

from enclave.common.config import (
    EnclaveConfig,
    MatrixConfig,
    UserMapping,
    load_config,
)


class TestDefaultConfig:
    """Test default configuration values."""

    def test_default_config_loads(self) -> None:
        config = EnclaveConfig()
        assert config.log_level == "INFO"
        assert config.matrix.device_name == "Enclave Bot"
        assert config.container.network == "none"
        assert config.container.userns == "keep-id"
        assert config.priv_broker.timeout == 300.0

    def test_default_container_image(self) -> None:
        config = EnclaveConfig()
        assert config.container.image == "enclave-agent:latest"

    def test_default_no_users(self) -> None:
        config = EnclaveConfig()
        assert config.users == []


class TestYamlLoading:
    """Test YAML config file loading."""

    def test_load_from_yaml(self, config_yaml: Path) -> None:
        config = load_config(config_yaml)
        assert config.matrix.homeserver == "https://matrix.test.com"
        assert config.matrix.user_id == "@testbot:test.com"
        assert config.matrix.password == "secret123"
        assert config.matrix.device_name == "Test Device"

    def test_load_users(self, config_yaml: Path) -> None:
        config = load_config(config_yaml)
        assert len(config.users) == 2
        assert config.users[0].matrix_id == "@alice:test.com"
        assert config.users[0].linux_user == "alice"
        assert config.users[0].max_sessions == 3
        assert config.users[1].can_approve_privilege is False

    def test_load_container_config(self, config_yaml: Path) -> None:
        config = load_config(config_yaml)
        assert config.container.image == "test-agent:latest"
        assert config.container.network == "none"

    def test_nonexistent_file_returns_defaults(self, tmp_path: Path) -> None:
        config = load_config(tmp_path / "nonexistent.yaml")
        assert config.matrix.homeserver == ""
        assert config.log_level == "INFO"

    def test_empty_yaml(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty.yaml"
        empty.write_text("")
        config = load_config(empty)
        assert config.matrix.homeserver == ""

    def test_partial_yaml(self, tmp_path: Path) -> None:
        partial = tmp_path / "partial.yaml"
        partial.write_text('log_level: "WARNING"\n')
        config = load_config(partial)
        assert config.log_level == "WARNING"
        assert config.matrix.homeserver == ""  # default


class TestEnvOverrides:
    """Test environment variable overrides."""

    def test_env_overrides_matrix(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ENCLAVE_MATRIX_HOMESERVER", "https://env.example.com")
        monkeypatch.setenv("ENCLAVE_MATRIX_USER", "@envbot:example.com")
        config = load_config()
        assert config.matrix.homeserver == "https://env.example.com"
        assert config.matrix.user_id == "@envbot:example.com"

    def test_env_overrides_yaml(
        self, config_yaml: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ENCLAVE_MATRIX_HOMESERVER", "https://override.com")
        config = load_config(config_yaml)
        # Env should override YAML
        assert config.matrix.homeserver == "https://override.com"
        # YAML values not overridden should remain
        assert config.matrix.user_id == "@testbot:test.com"

    def test_env_log_level(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ENCLAVE_LOG_LEVEL", "DEBUG")
        config = load_config()
        assert config.log_level == "DEBUG"


class TestUserMapping:
    """Test user mapping lookup."""

    def test_get_existing_user(self, config_yaml: Path) -> None:
        config = load_config(config_yaml)
        user = config.get_user_mapping("@alice:test.com")
        assert user is not None
        assert user.linux_user == "alice"

    def test_get_nonexistent_user(self, config_yaml: Path) -> None:
        config = load_config(config_yaml)
        user = config.get_user_mapping("@nobody:test.com")
        assert user is None

    def test_user_defaults(self) -> None:
        user = UserMapping(matrix_id="@test:test.com", linux_user="test")
        assert user.max_sessions == 5
        assert user.can_approve_privilege is True
        assert user.allowed_rooms == ["*"]


class TestNetworkConfig:
    """Test network configuration defaults."""

    def test_default_network_is_none(self) -> None:
        config = EnclaveConfig()
        assert config.container.network == "none"

    def test_copilot_network_default(self) -> None:
        config = EnclaveConfig()
        assert config.container.copilot_network == "slirp4netns"

    def test_dns_default_empty(self) -> None:
        config = EnclaveConfig()
        assert config.container.dns == ""
