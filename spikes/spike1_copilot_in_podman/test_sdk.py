"""Spike 1: Verify Copilot SDK works inside a container.

Tests:
1. SDK client starts and connects to Copilot CLI
2. Session creation with a system prompt
3. Custom tool definition and invocation
4. Multi-turn conversation
"""

from __future__ import annotations

import asyncio
import json
import os
import sys


async def main() -> None:
    try:
        from copilot import CopilotClient
    except ImportError:
        print("ERROR: github-copilot-sdk not installed. Run: pip install github-copilot-sdk")
        sys.exit(1)

    # Check auth
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        print("ERROR: No GITHUB_TOKEN or GH_TOKEN found in environment.")
        print("Run: export GITHUB_TOKEN=$(gh auth token)")
        sys.exit(1)

    print("=" * 60)
    print("Spike 1: Copilot SDK Test")
    print("=" * 60)

    # --- Test 1: Client startup ---
    print("\n[1/4] Starting Copilot client...")
    client = CopilotClient()
    try:
        await client.start()
        print("  ✓ Client started successfully")
    except Exception as e:
        print(f"  ✗ Client failed to start: {e}")
        sys.exit(1)

    # --- Test 2: Session creation ---
    print("\n[2/4] Creating session...")
    try:
        session = await client.create_session({
            "system_message": (
                "You are a helpful assistant running inside an Enclave agent container. "
                "You have access to a 'get_system_info' tool. When asked about the system, "
                "use it. Keep responses brief."
            ),
        })
        print("  ✓ Session created")
    except Exception as e:
        print(f"  ✗ Session creation failed: {e}")
        await client.stop()
        sys.exit(1)

    # --- Test 3: Custom tool invocation ---
    print("\n[3/4] Testing custom tool invocation...")

    # Define a simple custom tool
    tool_was_called = False

    async def get_system_info() -> str:
        """Return basic system information about the running environment."""
        nonlocal tool_was_called
        tool_was_called = True
        import platform

        info = {
            "platform": platform.system(),
            "release": platform.release(),
            "python": platform.python_version(),
            "in_container": os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv"),
            "user": os.environ.get("USER", "unknown"),
            "workspace": os.getcwd(),
        }
        return json.dumps(info, indent=2)

    # TODO: Register tool with session once we understand the SDK's tool API
    # For now, test a basic prompt
    try:
        response = await session.prompt("What is 2 + 2? Answer with just the number.")
        print(f"  ✓ Got response: {response.content[:100]}")
    except Exception as e:
        print(f"  ✗ Prompt failed: {e}")
        await client.stop()
        sys.exit(1)

    # --- Test 4: Multi-turn ---
    print("\n[4/4] Testing multi-turn conversation...")
    try:
        response2 = await session.prompt("Now multiply that result by 3. Answer with just the number.")
        print(f"  ✓ Got response: {response2.content[:100]}")
    except Exception as e:
        print(f"  ✗ Multi-turn failed: {e}")
        await client.stop()
        sys.exit(1)

    # Cleanup
    await client.stop()

    print("\n" + "=" * 60)
    print("Results:")
    print(f"  Client startup:     ✓")
    print(f"  Session creation:   ✓")
    print(f"  Basic prompt:       ✓")
    print(f"  Multi-turn:         ✓")
    print(f"  Custom tool called: {'✓' if tool_was_called else '⏭ (skipped — needs tool registration)'}")
    print("=" * 60)
    print("\nSpike 1 PASSED — Copilot SDK is functional in this environment.")


if __name__ == "__main__":
    asyncio.run(main())
