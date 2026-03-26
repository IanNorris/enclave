"""Plugin system for Enclave agent tools.

Allows users to define custom tools as Python files in a plugins directory.
Each plugin file should define one or more functions decorated with @plugin_tool.

Plugin files are auto-discovered from:
  - {workspace}/.enclave/plugins/     (project-level)
  - ~/.config/enclave/plugins/        (user-level)

Example plugin file (my_tool.py):

    from enclave.agent.plugins import plugin_tool

    @plugin_tool(
        name="greet",
        description="Greet someone by name",
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Name to greet"},
            },
            "required": ["name"],
        },
    )
    async def greet(params: dict) -> str:
        return f"Hello, {params['name']}!"
"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from enclave.common.logging import get_logger

log = get_logger("plugins")

# Registry of discovered plugin tools
_registry: list["PluginTool"] = []


@dataclass
class PluginTool:
    """A tool defined by a plugin."""

    name: str
    description: str
    handler: Callable
    parameters: dict[str, Any] = field(default_factory=lambda: {
        "type": "object", "properties": {},
    })
    source_file: str = ""


def plugin_tool(
    name: str,
    description: str,
    parameters: dict[str, Any] | None = None,
) -> Callable:
    """Decorator to register a function as a plugin tool.

    Args:
        name: Tool name (must be unique).
        description: Human-readable description.
        parameters: JSON Schema for the tool's parameters.
    """
    def decorator(func: Callable) -> Callable:
        tool = PluginTool(
            name=name,
            description=description,
            handler=func,
            parameters=parameters or {"type": "object", "properties": {}},
        )
        _registry.append(tool)
        return func
    return decorator


def discover_plugins(
    workspace: str = "/workspace",
    user_dir: str | None = None,
) -> list[PluginTool]:
    """Discover and load plugin tools from standard directories.

    Args:
        workspace: Agent workspace path.
        user_dir: User-level plugin directory (default: ~/.config/enclave/plugins/).

    Returns:
        List of discovered PluginTool objects.
    """
    _registry.clear()

    if user_dir is None:
        user_dir = str(Path.home() / ".config" / "enclave" / "plugins")

    plugin_dirs = [
        Path(workspace) / ".enclave" / "plugins",  # project-level
        Path(user_dir),                              # user-level
    ]

    for plugin_dir in plugin_dirs:
        if not plugin_dir.is_dir():
            continue

        for py_file in sorted(plugin_dir.glob("*.py")):
            if py_file.name.startswith("_"):
                continue
            try:
                _load_plugin_file(py_file)
            except Exception as e:
                log.warning("Failed to load plugin %s: %s", py_file, e)

    log.info("Discovered %d plugin tools", len(_registry))
    return list(_registry)


def _load_plugin_file(path: Path) -> None:
    """Load a single plugin Python file."""
    module_name = f"enclave_plugin_{path.stem}"

    spec = importlib.util.spec_from_file_location(module_name, str(path))
    if spec is None or spec.loader is None:
        log.warning("Cannot load plugin spec: %s", path)
        return

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module

    # Track which tools exist before loading
    before = len(_registry)
    spec.loader.exec_module(module)  # type: ignore
    after = len(_registry)

    # Tag newly registered tools with source file
    for tool in _registry[before:]:
        tool.source_file = str(path)

    loaded = after - before
    if loaded > 0:
        log.info("Loaded %d tools from %s", loaded, path.name)


def get_registered_tools() -> list[PluginTool]:
    """Get all currently registered plugin tools."""
    return list(_registry)


def clear_registry() -> None:
    """Clear all registered plugin tools (for testing)."""
    _registry.clear()
