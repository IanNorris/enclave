"""Command parser for Enclave control room.

Parses user messages into structured commands. Supports both
`!command args` and bare `command args` format for mobile friendliness.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class CommandType(str, Enum):
    """Known control room commands."""

    HELP = "help"
    PROJECT = "project"
    SESSIONS = "sessions"
    KILL = "kill"
    STATUS = "status"
    PERMS = "perms"
    REVOKE = "revoke"
    RULES = "rules"
    UNKNOWN = "unknown"


@dataclass
class ParsedCommand:
    """Result of parsing a user message."""

    command: CommandType
    args: list[str]
    raw_args: str
    raw_input: str

    @property
    def has_args(self) -> bool:
        return len(self.args) > 0


# Commands that take the rest of the line as a single argument (not split)
_SINGLE_ARG_COMMANDS = {CommandType.PROJECT, CommandType.KILL, CommandType.REVOKE}

# Help text for each command
COMMAND_HELP: dict[CommandType, str] = {
    CommandType.HELP: "Show this help message",
    CommandType.PROJECT: "Create a new project session — `project <name>`",
    CommandType.SESSIONS: "List active sessions",
    CommandType.KILL: "Stop a session — `kill <session-id>`",
    CommandType.STATUS: "Show system status",
    CommandType.PERMS: "List permissions — `perms [project]`",
    CommandType.REVOKE: "Revoke a permission — `revoke <permission-id>`",
    CommandType.RULES: "Manage permission rules — `rules [add|rm] [args]`",
}


def parse_command(text: str) -> ParsedCommand | None:
    """Parse a control room message into a command.

    Accepts both `!command args` and `command args` format.
    Returns None if the text is empty or whitespace-only.

    Args:
        text: Raw message text from the user.

    Returns:
        ParsedCommand if parseable, None if empty.
    """
    text = text.strip()
    if not text:
        return None

    # Strip leading ! if present
    if text.startswith("!"):
        text = text[1:]

    parts = text.split(None, 1)
    if not parts:
        return None

    cmd_word = parts[0].lower()
    rest = parts[1] if len(parts) > 1 else ""

    try:
        cmd_type = CommandType(cmd_word)
    except ValueError:
        return ParsedCommand(
            command=CommandType.UNKNOWN,
            args=[cmd_word] + (rest.split() if rest else []),
            raw_args=text,
            raw_input=text,
        )

    # For commands that take a name/id, keep the rest as a single arg
    if cmd_type in _SINGLE_ARG_COMMANDS:
        args = [rest.strip()] if rest.strip() else []
    else:
        args = rest.split() if rest else []

    return ParsedCommand(
        command=cmd_type,
        args=args,
        raw_args=rest,
        raw_input=text,
    )


def format_help() -> str:
    """Generate the help text shown to users.

    Returns:
        Formatted help string suitable for Matrix messages.
    """
    lines = ["**🏰 Enclave Commands**\n"]
    for cmd, description in COMMAND_HELP.items():
        lines.append(f"  `{cmd.value}` — {description}")
    lines.append("\nBoth `!command` and bare `command` are accepted.")
    return "\n".join(lines)
