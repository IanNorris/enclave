# Agent IPC API

Services running inside an Enclave container can communicate with the agent (and through it, the orchestrator) via a Unix domain socket using a simple newline-delimited JSON protocol.

## Socket Location

The IPC socket path is available in the environment variable:

```
IPC_SOCKET=/socket/orchestrator.sock
```

This socket is bind-mounted into every container at startup.

## Protocol

- **Transport**: Unix domain socket, stream-oriented
- **Framing**: One JSON object per line (newline-delimited, `\n`)
- **Encoding**: UTF-8

## Message Format

Every message is a JSON object with these fields:

```json
{
  "id": "uuid-string",
  "type": "message_type",
  "payload": { ... },
  "reply_to": null
}
```

| Field | Type | Description |
|-------|------|-------------|
| `id` | string | Unique message ID (UUID v4). Required. |
| `type` | string | Message type (see below). Required. |
| `payload` | object | Type-specific data. Required (can be `{}`). |
| `reply_to` | string\|null | If this is a reply, the `id` of the original message. |

## Sending a User Message to the Agent

The most common use case: a web service inside the container wants to notify the agent that something happened (e.g. a file was uploaded).

Send a `user_message` to the socket. The orchestrator will deliver it to the agent as if the user typed it.

### Python Example

```python
import json
import os
import socket
import uuid

def notify_agent(message: str) -> None:
    """Send a notification to the Enclave agent."""
    sock_path = os.environ.get("IPC_SOCKET", "/socket/orchestrator.sock")
    
    msg = {
        "id": str(uuid.uuid4()),
        "type": "user_message",
        "payload": {"content": message},
        "reply_to": None,
    }
    
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(sock_path)
        sock.sendall((json.dumps(msg) + "\n").encode())
    finally:
        sock.close()
```

### Usage

```python
# In your Flask/FastAPI/etc. handler:
notify_agent("New file uploaded: panic_data_2026-05-12.bin (4.2 KB)")
```

### Async Python Example

```python
import asyncio
import json
import os
import uuid

async def notify_agent(message: str) -> None:
    """Send a notification to the Enclave agent (async version)."""
    sock_path = os.environ.get("IPC_SOCKET", "/socket/orchestrator.sock")
    
    msg = {
        "id": str(uuid.uuid4()),
        "type": "user_message",
        "payload": {"content": message},
        "reply_to": None,
    }
    
    _, writer = await asyncio.open_unix_connection(sock_path)
    writer.write((json.dumps(msg) + "\n").encode())
    await writer.drain()
    writer.close()
    await writer.wait_closed()
```

## Message Types (Agent → Orchestrator)

These are the message types a service can send upstream:

| Type | Purpose | Payload |
|------|---------|---------|
| `user_message` | Deliver text to the agent as a user message | `{"content": "..."}` |
| `status_update` | Update the agent's status/activity display | `{"text": "..."}` |

## Important Notes

1. **Fire and forget** — Sending a `user_message` does not return a reply. The agent will process it on its next idle cycle.

2. **Message queuing** — If the agent is mid-turn (busy), messages are queued and delivered when the turn ends and the agent reaches idle state.

3. **Don't hold the connection open** — Connect, send your message, close. The socket is the orchestrator's listener; long-lived connections from services are not expected.

4. **No auth required** — The socket is container-local and only accessible within the same container, so no authentication is needed.

5. **Keep messages concise** — The message content becomes part of the agent's conversation context. Include just enough information for the agent to act on.

## Example: File Upload Notification

```python
from flask import Flask, request
import notify  # your module using the code above

app = Flask(__name__)

@app.route("/upload", methods=["POST"])
def upload():
    f = request.files["file"]
    path = f"/workspace/uploads/{f.filename}"
    f.save(path)
    
    notify.notify_agent(
        f"📁 New file received: {f.filename} "
        f"({os.path.getsize(path)} bytes) → {path}"
    )
    return {"ok": True, "path": path}
```

## Port Mapping

To make your service accessible from outside the container, use the agent's `request_port` tool first. The agent will map a host port to your container port:

- Container service listens on e.g. port `8080`
- Agent calls `request_port(container_port=8080, protocol="tcp")`
- Orchestrator maps `khione:9001` → container `8080/tcp`
- Users access `http://khione:9001`

Port mappings require a container restart to activate (podman limitation).
