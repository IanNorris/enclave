"""Tests for command parser."""

from enclave.orchestrator.commands import (
    CommandType,
    ParsedCommand,
    format_help,
    parse_command,
)


class TestParseCommand:
    """Test command parsing."""

    def test_bare_help(self) -> None:
        result = parse_command("help")
        assert result is not None
        assert result.command == CommandType.HELP
        assert result.args == []

    def test_bang_help(self) -> None:
        result = parse_command("!help")
        assert result is not None
        assert result.command == CommandType.HELP

    def test_project_with_name(self) -> None:
        result = parse_command("project My Cool Project")
        assert result is not None
        assert result.command == CommandType.PROJECT
        assert result.args == ["My Cool Project"]
        assert result.raw_args == "My Cool Project"

    def test_bang_project(self) -> None:
        result = parse_command("!project Test")
        assert result is not None
        assert result.command == CommandType.PROJECT
        assert result.args == ["Test"]

    def test_project_no_name(self) -> None:
        result = parse_command("project")
        assert result is not None
        assert result.command == CommandType.PROJECT
        assert result.args == []
        assert not result.has_args

    def test_sessions(self) -> None:
        result = parse_command("sessions")
        assert result is not None
        assert result.command == CommandType.SESSIONS

    def test_kill_with_id(self) -> None:
        result = parse_command("kill abc-123")
        assert result is not None
        assert result.command == CommandType.KILL
        assert result.args == ["abc-123"]

    def test_status(self) -> None:
        result = parse_command("!status")
        assert result is not None
        assert result.command == CommandType.STATUS

    def test_perms_with_project(self) -> None:
        result = parse_command("perms myproject")
        assert result is not None
        assert result.command == CommandType.PERMS
        assert result.args == ["myproject"]

    def test_perms_no_args(self) -> None:
        result = parse_command("perms")
        assert result is not None
        assert result.command == CommandType.PERMS
        assert result.args == []

    def test_revoke(self) -> None:
        result = parse_command("revoke perm-abc-123")
        assert result is not None
        assert result.command == CommandType.REVOKE
        assert result.args == ["perm-abc-123"]

    def test_rules_no_args(self) -> None:
        result = parse_command("rules")
        assert result is not None
        assert result.command == CommandType.RULES
        assert result.args == []

    def test_rules_add(self) -> None:
        result = parse_command("rules add *.github.com project")
        assert result is not None
        assert result.command == CommandType.RULES
        assert result.args == ["add", "*.github.com", "project"]

    def test_rules_rm(self) -> None:
        result = parse_command("rules rm rule-123")
        assert result is not None
        assert result.command == CommandType.RULES
        assert result.args == ["rm", "rule-123"]


class TestParseEdgeCases:
    """Test edge cases in parsing."""

    def test_empty_string(self) -> None:
        assert parse_command("") is None

    def test_whitespace_only(self) -> None:
        assert parse_command("   ") is None

    def test_case_insensitive(self) -> None:
        result = parse_command("HELP")
        assert result is not None
        assert result.command == CommandType.HELP

    def test_mixed_case(self) -> None:
        result = parse_command("Project Test")
        assert result is not None
        assert result.command == CommandType.PROJECT

    def test_extra_whitespace(self) -> None:
        result = parse_command("  !project   My Project  ")
        assert result is not None
        assert result.command == CommandType.PROJECT
        assert result.args == ["My Project"]

    def test_unknown_command(self) -> None:
        result = parse_command("foobar arg1 arg2")
        assert result is not None
        assert result.command == CommandType.UNKNOWN
        assert "foobar" in result.args

    def test_unknown_preserves_raw(self) -> None:
        result = parse_command("!gibberish some args here")
        assert result is not None
        assert result.command == CommandType.UNKNOWN
        assert result.raw_args == "gibberish some args here"

    def test_has_args_true(self) -> None:
        result = parse_command("project Foo")
        assert result is not None
        assert result.has_args is True

    def test_has_args_false(self) -> None:
        result = parse_command("help")
        assert result is not None
        assert result.has_args is False


class TestFormatHelp:
    """Test help text formatting."""

    def test_help_contains_all_commands(self) -> None:
        text = format_help()
        for cmd in CommandType:
            if cmd != CommandType.UNKNOWN:
                assert cmd.value in text

    def test_help_mentions_bang_prefix(self) -> None:
        text = format_help()
        assert "!command" in text

    def test_help_is_not_empty(self) -> None:
        assert len(format_help()) > 50
