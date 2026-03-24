"""Tests for logging setup."""

import logging

from enclave.common.logging import get_logger, setup_logging


class TestLogging:
    """Test logging configuration."""

    def test_setup_returns_logger(self) -> None:
        logger = setup_logging(level="DEBUG", name="test_enclave")
        assert isinstance(logger, logging.Logger)
        assert logger.name == "test_enclave"
        assert logger.level == logging.DEBUG

    def test_get_component_logger(self) -> None:
        setup_logging(name="enclave")
        logger = get_logger("matrix")
        assert logger.name == "enclave.matrix"

    def test_log_level_case_insensitive(self) -> None:
        logger = setup_logging(level="warning", name="test_case")
        assert logger.level == logging.WARNING

    def test_invalid_level_defaults_to_info(self) -> None:
        logger = setup_logging(level="INVALID", name="test_invalid")
        assert logger.level == logging.INFO

    def test_no_duplicate_handlers(self) -> None:
        setup_logging(name="test_dup")
        setup_logging(name="test_dup")
        logger = logging.getLogger("test_dup")
        assert len(logger.handlers) == 1
