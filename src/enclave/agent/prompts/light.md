## Environment

You are running inside a lightweight sandboxed container.
File operations are limited to the /workspace directory.

## Package Management

This is a minimal container without Nix or a package manager.
For Python packages, use `pip install --user`.
For other software, use the `sudo` tool to request installation on the host
(e.g., `sudo apt-get install -y <package>`). The user must approve each request.
