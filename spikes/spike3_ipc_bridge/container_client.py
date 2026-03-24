"""Spike 3: Container-side IPC client.

Connects to the orchestrator's Unix socket and exchanges JSON messages.
Runs inside a podman container with the socket bind-mounted in.

Protocol: newline-delimited JSON over Unix socket.
"""

import asyncio
import json
import os
import sys
import uuid


SOCKET_PATH = os.environ.get("IPC_SOCKET", "/socket/orchestrator.sock")


class ContainerClient:
    """Simulates an agent's IPC connection to the orchestrator."""

    def __init__(self, socket_path: str):
        self.socket_path = socket_path
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.results = {
            "connected": False,
            "hello_ack": False,
            "echo_works": False,
            "permission_flow": False,
            "server_push_received": False,
        }

    async def start(self) -> None:
        print(f"[agent] Connecting to {self.socket_path}...")

        try:
            self.reader, self.writer = await asyncio.open_unix_connection(
                self.socket_path
            )
        except (FileNotFoundError, ConnectionRefusedError) as e:
            print(f"[agent] ✗ Cannot connect: {e}")
            print(f"[agent]   Is the host server running?")
            sys.exit(1)

        self.results["connected"] = True
        print("[agent] ✓ Connected to orchestrator")

        # Test 1: Hello handshake
        print("\n[agent] Test 1: Hello handshake...")
        await self._send({"type": "hello", "agent_id": "spike3-test", "version": "0.1.0"})
        resp = await self._recv()
        if resp and resp.get("type") == "hello_ack":
            self.results["hello_ack"] = True
            print(f"[agent] ✓ Server: {resp.get('server')} v{resp.get('version')}")
            print(f"[agent]   Capabilities: {resp.get('capabilities')}")
        else:
            print(f"[agent] ✗ Unexpected response: {resp}")

        # Test 2: Echo round-trip
        print("\n[agent] Test 2: Echo round-trip...")
        test_data = {"message": "ping", "timestamp": 12345}
        await self._send({"type": "echo", "data": test_data})
        resp = await self._recv()
        if resp and resp.get("type") == "echo_reply" and resp.get("data") == test_data:
            self.results["echo_works"] = True
            print("[agent] ✓ Echo data matches")
        else:
            print(f"[agent] ✗ Echo mismatch: {resp}")

        # Test 3: Permission request flow
        print("\n[agent] Test 3: Permission request...")
        request_id = str(uuid.uuid4())
        await self._send({
            "type": "permission_request",
            "request_id": request_id,
            "path": "/home/ian/projects/myapp",
            "access": "read-write",
        })
        resp = await self._recv()
        if (
            resp
            and resp.get("type") == "permission_response"
            and resp.get("request_id") == request_id
            and resp.get("approved") is True
        ):
            self.results["permission_flow"] = True
            print(f"[agent] ✓ Permission approved for {resp.get('path')}")
        else:
            print(f"[agent] ✗ Permission response unexpected: {resp}")

        # Test 4: Server-initiated push
        print("\n[agent] Test 4: Server push (simulated user message)...")
        await self._send({"type": "ready"})
        resp = await self._recv()
        if resp and resp.get("type") == "user_message":
            self.results["server_push_received"] = True
            print(f"[agent] ✓ Received: \"{resp.get('content')}\"")
            print(f"[agent]   From: {resp.get('sender')}")
        else:
            print(f"[agent] ✗ Expected user_message, got: {resp}")

        # Request shutdown
        await self._send({"type": "shutdown"})

        # Print results
        print("\n" + "=" * 60)
        print("Spike 3: IPC Bridge — Container Client Results:")
        for k, v in self.results.items():
            print(f"  {'✓' if v else '✗'} {k}")
        print("=" * 60)

        all_passed = all(self.results.values())
        if all_passed:
            print("\n🎉 Spike 3 AGENT PASSED — IPC bridge fully functional!")
        else:
            failed = [k for k, v in self.results.items() if not v]
            print(f"\n⚠  Spike 3 AGENT PARTIAL — failed: {', '.join(failed)}")

        self.writer.close()
        await self.writer.wait_closed()

        sys.exit(0 if all_passed else 1)

    async def _send(self, msg: dict) -> None:
        """Send a JSON message to the orchestrator."""
        data = json.dumps(msg) + "\n"
        self.writer.write(data.encode())
        await self.writer.drain()

    async def _recv(self) -> dict | None:
        """Receive a JSON message from the orchestrator."""
        try:
            line = await asyncio.wait_for(self.reader.readline(), timeout=10.0)
            if not line:
                return None
            return json.loads(line.decode().strip())
        except asyncio.TimeoutError:
            print("[agent] ⚠ Timeout waiting for response")
            return None


async def main() -> None:
    print("=" * 60)
    print("Spike 3: IPC Bridge — Container Client")
    print("=" * 60)
    print(f"  Socket: {SOCKET_PATH}")
    print(f"  Hostname: {os.uname().nodename}")
    print(f"  UID: {os.getuid()}")

    client = ContainerClient(SOCKET_PATH)
    await client.start()


if __name__ == "__main__":
    asyncio.run(main())
