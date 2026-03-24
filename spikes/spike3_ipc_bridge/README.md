# Spike 3: IPC Bridge

Prove that we can reliably communicate between a host process and a podman
container via Unix domain sockets.

## What we're testing

1. Host creates a Unix socket and passes it into a container
2. Container connects to the socket
3. Bidirectional JSON message passing works
4. Async message handling (both sides can send without blocking)
5. Connection recovery on disconnect

## Running

```bash
# Terminal 1: Start the host-side server
python host.py

# Terminal 2: Start the container-side client in podman
podman run --rm -it \
    --userns=keep-id \
    -v /tmp/enclave-spike3.sock:/socket/orchestrator.sock \
    -v $(pwd)/container_client.py:/workspace/client.py:ro \
    python:3.12-slim python /workspace/client.py
```

## Success criteria

- [ ] Socket passes through container boundary
- [ ] Host sends message → container receives it
- [ ] Container sends message → host receives it
- [ ] Multiple messages in rapid succession
- [ ] Clean shutdown on both sides
