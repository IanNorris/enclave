"""Tests for the privilege broker client."""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

import pytest

from enclave.orchestrator.priv_client import PrivBrokerClient, PrivBrokerResult


class TestPrivBrokerResult:
    def test_success_result(self) -> None:
        r = PrivBrokerResult({"id": "r1", "success": True, "exit_code": 0})
        assert r.success is True
        assert r.exit_code == 0

    def test_error_result(self) -> None:
        r = PrivBrokerResult({"id": "r1", "success": False, "error": "denied"})
        assert r.success is False
        assert r.error == "denied"

    def test_exec_result(self) -> None:
        r = PrivBrokerResult({
            "id": "r1",
            "success": True,
            "exit_code": 0,
            "stdout": "hello",
            "stderr": "",
        })
        assert r.stdout == "hello"
        assert r.stderr == ""


class TestPrivBrokerClient:
    @pytest.mark.asyncio
    async def test_connect_to_nonexistent(self) -> None:
        client = PrivBrokerClient("/nonexistent/socket.sock")
        ok = await client.connect()
        assert ok is False

    @pytest.mark.asyncio
    async def test_ping_with_mock_server(self) -> None:
        """Test ping against a mock broker server."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sock_path = str(Path(tmpdir) / "broker.sock")

            async def handle_client(reader, writer):
                line = await reader.readline()
                req = json.loads(line.decode().strip())
                resp = {"id": req["id"], "success": True, "exit_code": 0}
                writer.write((json.dumps(resp) + "\n").encode())
                await writer.drain()
                writer.close()

            server = await asyncio.start_unix_server(handle_client, path=sock_path)

            client = PrivBrokerClient(sock_path)
            ok = await client.connect()
            assert ok is True

            result = await client.ping()
            assert result is True

            await client.disconnect()
            server.close()
            await server.wait_closed()

    @pytest.mark.asyncio
    async def test_exec_with_mock_server(self) -> None:
        """Test command execution against a mock broker."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sock_path = str(Path(tmpdir) / "broker.sock")

            async def handle_client(reader, writer):
                line = await reader.readline()
                req = json.loads(line.decode().strip())
                assert req["type"] == "exec"
                assert req["command"] == "echo"
                resp = {
                    "id": req["id"],
                    "success": True,
                    "exit_code": 0,
                    "stdout": "hello\n",
                    "stderr": "",
                }
                writer.write((json.dumps(resp) + "\n").encode())
                await writer.drain()
                writer.close()

            server = await asyncio.start_unix_server(handle_client, path=sock_path)

            client = PrivBrokerClient(sock_path)
            await client.connect()

            result = await client.exec_command("s1", "echo", ["hello"])
            assert result.success is True
            assert result.stdout == "hello\n"

            await client.disconnect()
            server.close()
            await server.wait_closed()

    @pytest.mark.asyncio
    async def test_mount_with_mock_server(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            sock_path = str(Path(tmpdir) / "broker.sock")

            async def handle_client(reader, writer):
                line = await reader.readline()
                req = json.loads(line.decode().strip())
                assert req["type"] == "mount"
                resp = {"id": req["id"], "success": True, "exit_code": 0}
                writer.write((json.dumps(resp) + "\n").encode())
                await writer.drain()
                writer.close()

            server = await asyncio.start_unix_server(handle_client, path=sock_path)

            client = PrivBrokerClient(sock_path)
            await client.connect()

            result = await client.mount("s1", "/home/a", "/workspace/a")
            assert result.success is True

            await client.disconnect()
            server.close()
            await server.wait_closed()

    def test_not_connected(self) -> None:
        client = PrivBrokerClient("/fake")
        assert client.is_connected is False
