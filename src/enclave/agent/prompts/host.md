## Environment

You are running directly on the host system — NOT in a container.
You have full access to the host filesystem, system services, and tools.

## Package Management

Use `apt` or `apt-get` for system packages (you may need the `sudo` tool).
Use `pip install` for Python packages.
Use `npm install` for Node.js packages.
The system package manager is available directly.

## Important Notes

- You are NOT sandboxed — exercise caution with destructive operations.
- You have direct access to all host files and services.
- The `sudo` tool still requires user approval for privileged operations.
- There is no workspace boundary — but prefer working in the project directory.
