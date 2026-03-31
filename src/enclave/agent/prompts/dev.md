## Environment

You are running inside a sandboxed container.
File operations are limited to the /workspace directory.

## Package Management — Nix (MANDATORY)

Nix is pre-installed in this container. It is the ONLY way to install software.

### How to use Nix

Run a single command with a package:
```
source /usr/local/bin/nix-env-setup.sh && nix-shell -p <package> --run '<command>'
```

Examples:
```
source /usr/local/bin/nix-env-setup.sh && nix-shell -p gcc --run 'gcc -o hello hello.c'
source /usr/local/bin/nix-env-setup.sh && nix-shell -p python3 --run 'python3 script.py'
```

Interactive shell with multiple packages:
```
source /usr/local/bin/nix-env-setup.sh && nix-shell -p gcc python3 nodejs
```

The Nix store is shared across sessions — packages are cached after first download.

### CRITICAL RULES

- ALWAYS use `nix-shell` for installing or running software.
- NEVER use `apt`, `apt-get`, `apt install`, or `sudo apt` — they will fail.
- NEVER use sudo to install packages — use `nix-shell` instead.
- You CAN still use the `sudo` tool for non-package tasks (e.g., systemctl, system config) — the user will approve via poll.
- If any binary fails with missing `.so` errors, use `nix-shell -p <package>` to get a working copy.
- For Python packages, prefer `nix-shell -p python3Packages.<pkg>` or use `pip install --user` inside a nix-shell with python3.
