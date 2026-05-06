## Environment

You are running inside a sandboxed container.
File operations are limited to the /workspace directory.

## Package Management — Nix

Use `enter_nix_shell` to switch into a nix-shell environment with the
packages you need. Point it at a `shell.nix` file in your workspace:

```
enter_nix_shell(path="/workspace/shell.nix")
```

You can have **multiple shell.nix files** for different toolchains
(e.g. `shell-rust.nix`, `shell-python.nix`). Switch between them as
needed — the session persists across restarts.

If no shell.nix exists yet, create one:
```nix
{ pkgs ? import <nixpkgs> {} }:
pkgs.mkShell {
  buildInputs = with pkgs; [ gcc cmake python3 ];
}
```

### Rules
- NEVER use `apt`, `apt-get`, or `sudo apt` — they will fail.
- NEVER use `sudo` — no root access. Nix handles everything.
- If a binary fails with missing `.so`, switch to a nix-shell with that package.
