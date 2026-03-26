"""Tests for the plugin system."""

from __future__ import annotations

from pathlib import Path

import pytest

from enclave.agent.plugins import (
    PluginTool,
    clear_registry,
    discover_plugins,
    get_registered_tools,
    plugin_tool,
)


@pytest.fixture(autouse=True)
def clean_registry():
    """Clear plugin registry before/after each test."""
    clear_registry()
    yield
    clear_registry()


class TestPluginDecorator:
    """Tests for the @plugin_tool decorator."""

    def test_registers_tool(self) -> None:
        @plugin_tool(
            name="test_tool",
            description="A test tool",
        )
        async def my_tool(params: dict) -> str:
            return "hello"

        tools = get_registered_tools()
        assert len(tools) == 1
        assert tools[0].name == "test_tool"
        assert tools[0].description == "A test tool"

    def test_registers_with_parameters(self) -> None:
        @plugin_tool(
            name="param_tool",
            description="Tool with params",
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                },
                "required": ["name"],
            },
        )
        async def my_tool(params: dict) -> str:
            return params["name"]

        tools = get_registered_tools()
        assert tools[0].parameters["required"] == ["name"]

    def test_multiple_tools(self) -> None:
        @plugin_tool(name="tool_a", description="Tool A")
        async def tool_a(params: dict) -> str:
            return "a"

        @plugin_tool(name="tool_b", description="Tool B")
        async def tool_b(params: dict) -> str:
            return "b"

        tools = get_registered_tools()
        assert len(tools) == 2


class TestPluginDiscovery:
    """Tests for plugin discovery from filesystem."""

    def test_discover_from_project_plugins(self, tmp_path: Path) -> None:
        """Discover plugins from workspace/.enclave/plugins/."""
        plugins_dir = tmp_path / ".enclave" / "plugins"
        plugins_dir.mkdir(parents=True)

        # Create a plugin file
        plugin = plugins_dir / "greet.py"
        plugin.write_text("""
from enclave.agent.plugins import plugin_tool

@plugin_tool(
    name="greet",
    description="Greet someone",
    parameters={"type": "object", "properties": {"name": {"type": "string"}}},
)
async def greet(params):
    return f"Hello, {params['name']}!"
""")

        tools = discover_plugins(workspace=str(tmp_path), user_dir=str(tmp_path / "nonexistent"))
        assert len(tools) == 1
        assert tools[0].name == "greet"
        assert "greet.py" in tools[0].source_file

    def test_discover_from_user_dir(self, tmp_path: Path) -> None:
        """Discover plugins from user config dir."""
        user_plugins = tmp_path / "user_plugins"
        user_plugins.mkdir()

        plugin = user_plugins / "my_tool.py"
        plugin.write_text("""
from enclave.agent.plugins import plugin_tool

@plugin_tool(name="user_tool", description="User tool")
async def user_tool(params):
    return "user result"
""")

        tools = discover_plugins(
            workspace=str(tmp_path / "no_workspace"),
            user_dir=str(user_plugins),
        )
        assert len(tools) == 1
        assert tools[0].name == "user_tool"

    def test_discover_both_dirs(self, tmp_path: Path) -> None:
        """Plugins from both project and user dirs are discovered."""
        proj_plugins = tmp_path / "workspace" / ".enclave" / "plugins"
        proj_plugins.mkdir(parents=True)
        user_plugins = tmp_path / "user_plugins"
        user_plugins.mkdir()

        (proj_plugins / "proj_tool.py").write_text("""
from enclave.agent.plugins import plugin_tool

@plugin_tool(name="proj_tool", description="Project tool")
async def proj_tool(params):
    return "proj"
""")

        (user_plugins / "user_tool.py").write_text("""
from enclave.agent.plugins import plugin_tool

@plugin_tool(name="user_tool", description="User tool")
async def user_tool(params):
    return "user"
""")

        tools = discover_plugins(
            workspace=str(tmp_path / "workspace"),
            user_dir=str(user_plugins),
        )
        assert len(tools) == 2
        names = {t.name for t in tools}
        assert names == {"proj_tool", "user_tool"}

    def test_ignore_underscored_files(self, tmp_path: Path) -> None:
        """Files starting with _ are ignored."""
        plugins_dir = tmp_path / ".enclave" / "plugins"
        plugins_dir.mkdir(parents=True)

        (plugins_dir / "__init__.py").write_text("# ignored")
        (plugins_dir / "_helper.py").write_text("""
from enclave.agent.plugins import plugin_tool

@plugin_tool(name="hidden", description="Should not load")
async def hidden(params):
    return "hidden"
""")

        tools = discover_plugins(workspace=str(tmp_path))
        assert len(tools) == 0

    def test_no_plugins_dir(self, tmp_path: Path) -> None:
        """No error when plugins directory doesn't exist."""
        tools = discover_plugins(
            workspace=str(tmp_path / "nonexistent"),
            user_dir=str(tmp_path / "also_nonexistent"),
        )
        assert len(tools) == 0

    def test_broken_plugin_skipped(self, tmp_path: Path) -> None:
        """A plugin with syntax errors is skipped gracefully."""
        plugins_dir = tmp_path / ".enclave" / "plugins"
        plugins_dir.mkdir(parents=True)

        # Good plugin
        (plugins_dir / "good.py").write_text("""
from enclave.agent.plugins import plugin_tool

@plugin_tool(name="good", description="Works fine")
async def good(params):
    return "ok"
""")

        # Bad plugin (syntax error)
        (plugins_dir / "bad.py").write_text("def broken(:\n    pass\n")

        tools = discover_plugins(workspace=str(tmp_path))
        assert len(tools) == 1
        assert tools[0].name == "good"

    def test_clear_registry(self) -> None:
        """clear_registry removes all tools."""
        @plugin_tool(name="temp", description="Temporary")
        async def temp(params):
            return "temp"

        assert len(get_registered_tools()) == 1
        clear_registry()
        assert len(get_registered_tools()) == 0
