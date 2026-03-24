# Spike 1: Copilot SDK in Podman

Prove that the GitHub Copilot SDK can run inside a rootless podman container.

## What we're testing

1. Copilot CLI starts in server mode inside a container
2. SDK connects to the CLI and creates a session
3. Custom tools work (agent can call Python functions we define)
4. Multi-turn conversation works
5. Auth works via `GITHUB_TOKEN` environment variable

## Prerequisites

- `podman` installed on host
- A valid GitHub token with Copilot access
- Copilot CLI binary (we'll copy it into the container)

## Running

```bash
# Build the container
podman build -t enclave-spike1 .

# Run with your GitHub token
podman run --rm -it \
    --userns=keep-id \
    -e GITHUB_TOKEN="$(gh auth token)" \
    enclave-spike1

# Or run the test directly on the host first (no container)
pip install github-copilot-sdk
python test_sdk.py
```

## Success criteria

- [ ] SDK session created successfully
- [ ] Agent responds to a prompt
- [ ] Custom tool gets called by the agent
- [ ] Works inside a podman container
