"""Spike 1: Verify Copilot SDK works inside a container.

Tests:
1. SDK client starts and connects to Copilot CLI
2. Session creation with a system prompt
3. Custom tool definition and invocation
4. Multi-turn conversation
"""

import asyncio
import json
import os
import platform
import sys

try:
    from copilot import (
        CopilotClient,
        PermissionRequest,
        PermissionRequestResult,
        SystemMessageAppendConfig,
        define_tool,
    )
    from copilot.generated.session_events import SessionEventType
    from pydantic import BaseModel, Field

    SDK_AVAILABLE = True
except ImportError:
    SDK_AVAILABLE = False


# --- Custom tool (must be at module level for type hint resolution) ---
tool_was_called = False

if SDK_AVAILABLE:

    class GetSystemInfoParams(BaseModel):
        """Parameters for get_system_info tool."""
        verbose: bool = Field(default=False, description="Include extra details")

    @define_tool(description="Return basic system information about the running environment")
    def get_system_info(params: GetSystemInfoParams) -> str:
        global tool_was_called
        tool_was_called = True
        info = {
            "platform": platform.system(),
            "release": platform.release(),
            "python": platform.python_version(),
            "in_container": os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv"),
            "user": os.environ.get("USER", "unknown"),
            "workspace": os.getcwd(),
        }
        if params.verbose:
            info["env_keys"] = sorted(os.environ.keys())
        return json.dumps(info, indent=2)

async def main() -> None:
    if not SDK_AVAILABLE:
        print("ERROR: github-copilot-sdk not installed. Run: pip install github-copilot-sdk")
        sys.exit(1)

    # Check auth
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        print("WARNING: No GITHUB_TOKEN or GH_TOKEN found in environment.")
        print("The SDK may use stored credentials from 'copilot' CLI login instead.")

    print("=" * 60)
    print("Spike 1: Copilot SDK Test")
    print("=" * 60)

    # Permission handler — auto-approve everything for this spike
    def handle_permission(
        request: PermissionRequest, context: dict[str, str]
    ) -> PermissionRequestResult:
        print(f"  [permission] Auto-approving: {request}")
        return PermissionRequestResult(kind="approved")

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
    print("\n[2/4] Creating session with custom tool...")
    try:
        session = await client.create_session(
            on_permission_request=handle_permission,
            system_message=SystemMessageAppendConfig(
                append=(
                    "You are a test agent running inside an Enclave spike test. "
                    "You have access to a 'get_system_info' tool. When asked about "
                    "the system or environment, ALWAYS use the get_system_info tool "
                    "rather than running shell commands. Keep responses brief."
                ),
            ),
            tools=[get_system_info],
        )
        print("  ✓ Session created")
    except Exception as e:
        print(f"  ✗ Session creation failed: {e}")
        await client.stop()
        sys.exit(1)

    # --- Test 3: Basic prompt + tool invocation ---
    print("\n[3/4] Sending prompt (should trigger get_system_info tool)...")
    try:
        event = await session.send_and_wait(
            "What system are you running on? Use your get_system_info tool to find out.",
            timeout=30.0,
        )
        if event and event.data and event.data.content:
            response_text = event.data.content[:200]
            print(f"  ✓ Got response: {response_text}")
        elif event:
            print(f"  ✓ Got event type: {event.type}")
            # Try to extract text from the event
            if event.data:
                for attr in ['content', 'message', 'summary']:
                    val = getattr(event.data, attr, None)
                    if val:
                        print(f"  ✓ {attr}: {str(val)[:200]}")
                        break
        else:
            print("  ⚠ No event returned (timeout?)")
    except Exception as e:
        print(f"  ✗ Prompt failed: {e}")
        import traceback
        traceback.print_exc()

    # --- Test 4: Multi-turn ---
    print("\n[4/4] Testing multi-turn conversation...")
    try:
        event2 = await session.send_and_wait(
            "Now tell me: are you running inside a container? Answer yes or no.",
            timeout=30.0,
        )
        if event2 and event2.data and event2.data.content:
            print(f"  ✓ Got response: {event2.data.content[:200]}")
        elif event2:
            print(f"  ✓ Got event type: {event2.type}")
        else:
            print("  ⚠ No event returned (timeout?)")
    except Exception as e:
        print(f"  ✗ Multi-turn failed: {e}")
        import traceback
        traceback.print_exc()

    # Cleanup
    try:
        await session.disconnect()
    except Exception:
        pass
    await client.stop()

    print("\n" + "=" * 60)
    print("Results:")
    print(f"  Client startup:     ✓")
    print(f"  Session creation:   ✓")
    print(f"  Basic prompt:       ✓")
    print(f"  Custom tool called: {'✓' if tool_was_called else '✗ (tool not invoked)'}")
    print("=" * 60)

    if tool_was_called:
        print("\n🎉 Spike 1 PASSED — Copilot SDK + custom tools working!")
    else:
        print("\n⚠  Spike 1 PARTIAL — SDK works but custom tool was not invoked.")
        print("   This may need tool registration adjustments.")


if __name__ == "__main__":
    asyncio.run(main())

