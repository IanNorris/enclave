# Agent IPC API

Services running inside an Enclave container can notify the agent about events using a simple file-based inbox mechanism.

## How It Works

The agent monitors an inbox directory for notification files. When a service drops a JSON file into the inbox, the agent picks it up and processes it as a user message on its next idle cycle.

## Inbox Location

```
/workspace/.enclave/inbox/
```

The agent creates this directory on startup. Services write `.json` files here.

## Message Format

Each notification is a JSON file with any unique filename (e.g. timestamp-based):

```json
{
  "content": "New file uploaded: panic_data_2026-05-12.bin (4.2 KB)",
  "timestamp": "2026-05-12T00:15:00Z"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `content` | string | Yes | The message text delivered to the agent |
| `timestamp` | string | No | ISO 8601 timestamp (informational) |

## Python Example

```python
import json
import os
import time
from pathlib import Path

INBOX_DIR = Path("/workspace/.enclave/inbox")

def notify_agent(message: str) -> None:
    """Send a notification to the Enclave agent."""
    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    
    filename = f"{int(time.time() * 1000)}.json"
    msg = {"content": message, "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    
    # Write atomically (write to temp, then rename)
    tmp_path = INBOX_DIR / f".{filename}.tmp"
    target_path = INBOX_DIR / filename
    tmp_path.write_text(json.dumps(msg))
    tmp_path.rename(target_path)
```

## Usage in a Web Service

```python
from flask import Flask, request

app = Flask(__name__)

@app.route("/upload", methods=["POST"])
def upload():
    f = request.files["file"]
    path = f"/workspace/uploads/{f.filename}"
    f.save(path)
    
    notify_agent(
        f"📁 New file received: {f.filename} "
        f"({os.path.getsize(path)} bytes) → {path}"
    )
    return {"ok": True, "path": path}
```

## Important Notes

1. **Atomic writes** — Always write to a temp file then rename, to avoid the agent reading a partial file.

2. **Agent polling** — The agent checks the inbox directory periodically and at the start of each idle cycle. Delivery is near-instant when idle, or at the next turn boundary when busy.

3. **Files are consumed** — The agent deletes notification files after processing them.

4. **Keep messages concise** — The content becomes part of the agent's conversation context.

5. **No direct socket access** — Do NOT connect to the IPC socket (`/socket/orchestrator.sock`) directly. It supports only one connection per session; a second connection will kill the agent's IPC link.

## Port Mapping

To make your service accessible from outside the container, ask the agent to use the `request_port` tool:

- Container service listens on e.g. port `8080`
- Agent calls `request_port(container_port=8080, protocol="tcp")`
- Orchestrator maps `khione:9001` → container `8080/tcp`
- Users access `http://khione:9001`

Port mappings require a container restart to activate (podman limitation).
