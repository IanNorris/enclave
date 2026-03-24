"""Spike 3: Host-side IPC server.

Listens on a Unix socket and handles JSON messages from the container agent.
Proves bidirectional communication between host orchestrator and podman container.

Protocol: newline-delimited JSON over Unix socket.
Each message is a single JSON object terminated by \n.
"""

import asyncio
import json
import os
import signal
import sys
from pathlib import Path


SOCKET_PATH = os.environ.get("IPC_SOCKET", "/tmp/enclave-spike3.sock")


class HostServer:
    """Simulates the orchestrator's IPC endpoint."""

    def __init__(self, socket_path: str):
        self.socket_path = socket_path
        self.server: asyncio.Server | None = None
        self.clients: dict[str, asyncio.StreamWriter] = {}
        self.results = {
            "server_started": False,
            "client_connected": False,
            "message_received": False,
            "response_sent": False,
            "server_push": False,
            "permission_flow": False,
        }

    async def start(self) -> None:
        # Clean up stale socket
        sock = Path(self.socket_path)
        if sock.exists():
            sock.unlink()

        self.server = await asyncio.start_unix_server(
            self._handle_client, path=self.socket_path
        )
        # Make socket accessible (in production, restrict to specific user)
        os.chmod(self.socket_path, 0o777)

        self.results["server_started"] = True
        print(f"[host] ✓ Server listening on {self.socket_path}")
        print("[host]   Waiting for container client to connect...")

        # Set up clean shutdown
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda: asyncio.create_task(self.shutdown()))

        async with self.server:
            await self.server.serve_forever()

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        client_id = f"client-{len(self.clients)}"
        self.clients[client_id] = writer
        self.results["client_connected"] = True
        print(f"[host] ✓ Client connected: {client_id}")

        try:
            while True:
                line = await reader.readline()
                if not line:
                    break

                msg = json.loads(line.decode().strip())
                print(f"[host]   ← Received: {msg['type']}")
                self.results["message_received"] = True

                response = await self._handle_message(msg, client_id)
                if response:
                    await self._send(writer, response)
                    self.results["response_sent"] = True

        except (json.JSONDecodeError, ConnectionResetError) as e:
            print(f"[host]   ⚠ Client error: {e}")
        finally:
            del self.clients[client_id]
            writer.close()
            await writer.wait_closed()
            print(f"[host]   Client disconnected: {client_id}")

    async def _handle_message(self, msg: dict, client_id: str) -> dict | None:
        """Route messages and return responses."""
        msg_type = msg.get("type")

        if msg_type == "hello":
            return {
                "type": "hello_ack",
                "server": "enclave-orchestrator",
                "version": "0.1.0",
                "capabilities": ["permission", "privilege", "file_access"],
            }

        elif msg_type == "echo":
            return {"type": "echo_reply", "data": msg.get("data")}

        elif msg_type == "permission_request":
            # Simulate the approval flow
            path = msg.get("path", "/unknown")
            print(f"[host]   📋 Permission request for: {path}")
            print(f"[host]   → Auto-approving (spike test)")
            self.results["permission_flow"] = True
            return {
                "type": "permission_response",
                "request_id": msg.get("request_id"),
                "path": path,
                "approved": True,
            }

        elif msg_type == "ready":
            # Client is ready — send a server-initiated push
            print("[host]   → Sending server push (simulated user message)...")
            writer = self.clients.get(client_id)
            if writer:
                await self._send(writer, {
                    "type": "user_message",
                    "content": "Hello from the host! This is a simulated user message.",
                    "sender": "@test:matrix.localhost",
                })
                self.results["server_push"] = True
            return None

        elif msg_type == "shutdown":
            print("[host]   → Client requested shutdown")
            await self.shutdown()
            return None

        else:
            return {"type": "error", "message": f"Unknown message type: {msg_type}"}

    async def _send(self, writer: asyncio.StreamWriter, msg: dict) -> None:
        """Send a JSON message to a client."""
        data = json.dumps(msg) + "\n"
        writer.write(data.encode())
        await writer.drain()
        print(f"[host]   → Sent: {msg['type']}")

    async def shutdown(self) -> None:
        print("\n[host] Shutting down...")
        print("\n[host] Results:")
        for k, v in self.results.items():
            print(f"  {'✓' if v else '✗'} {k}")

        all_passed = all(self.results.values())
        if all_passed:
            print("\n🎉 Spike 3 HOST PASSED")
        else:
            failed = [k for k, v in self.results.items() if not v]
            print(f"\n⚠  Spike 3 HOST PARTIAL — not tested: {', '.join(failed)}")

        # Close all clients
        for writer in self.clients.values():
            writer.close()

        if self.server:
            self.server.close()

        # Clean up socket
        sock = Path(self.socket_path)
        if sock.exists():
            sock.unlink()

        sys.exit(0)


async def main() -> None:
    print("=" * 60)
    print("Spike 3: IPC Bridge — Host Server")
    print("=" * 60)

    server = HostServer(SOCKET_PATH)
    await server.start()


if __name__ == "__main__":
    asyncio.run(main())
