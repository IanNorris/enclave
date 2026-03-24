"""Structured logging for Enclave.

Provides consistent JSON-formatted logging across all components.
"""

import logging
import sys


LOG_FORMAT = "%(asctime)s [%(name)s] %(levelname)s: %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_logging(level: str = "INFO", name: str = "enclave") -> logging.Logger:
    """Configure and return the root enclave logger.

    Args:
        level: Log level string (DEBUG, INFO, WARNING, ERROR).
        name: Logger name prefix.

    Returns:
        Configured logger instance.
    """
    logger = logging.getLogger(name)

    if logger.handlers:
        return logger

    logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT))
    logger.addHandler(handler)

    return logger


def get_logger(component: str) -> logging.Logger:
    """Get a child logger for a specific component.

    Args:
        component: Component name (e.g., 'matrix', 'container', 'ipc').

    Returns:
        Logger instance under the enclave namespace.
    """
    return logging.getLogger(f"enclave.{component}")
