# Talking to a QEMU console from a container agent

An Enclave agent (e.g. Brook) runs inside a container, but QEMU launched via the
`launch_gui` tool runs on the **host**. This document describes the convention
for giving the agent a console/monitor channel into that VM.

## How it works

The session workspace is a **bidirectional** bind mount (`-v <host_ws>:/workspace`),
and unix-domain sockets placed on it are reachable from both sides (the same
inode). This is the same mechanism the orchestrator already uses for its IPC
socket. So: **QEMU exposes its console(s) as unix sockets inside the workspace,
and the agent connects to them from the container.**

`--userns keep-id` maps container uid 1000 → host uid 1000, so a socket QEMU
creates as the host user is connectable from the container. This has been
verified end-to-end (container → host QMP/HMP through the mount).

The `qemu-console` helper (`/usr/local/bin/qemu-console`, stdlib-only) handles
the QMP handshake and the agent's one-shot-shell limitation.

## 1. Launch QEMU with console sockets

In the launch script you hand to `launch_gui`, add (agent writes `/workspace/…`;
the orchestrator translates it to the host path QEMU needs):

```sh
qemu-system-x86_64 ... \
    -qmp    unix:/workspace/qmp.sock,server,nowait \
    -serial unix:/workspace/serial.sock,server,nowait
```

- `-qmp` — the machine protocol (JSON). Control the VM: status, `cont`,
  `screendump`, `sendkey`, `query-*`, and any monitor command via
  `human-monitor-command`. Best for automation/profiling.
- `-serial` — the guest's serial console (the OS/app inside the VM).

## 2. Drive the VM (QMP / HMP — stateless, one command per call)

```sh
# Raw QMP command (execute-name or full JSON). Prints the JSON reply.
qemu-console qmp /workspace/qmp.sock query-status
qemu-console qmp /workspace/qmp.sock '{"execute":"cont"}'

# Human monitor command via QMP — prints the console text.
qemu-console hmp /workspace/qmp.sock "info registers"
qemu-console hmp /workspace/qmp.sock "info status"
qemu-console hmp /workspace/qmp.sock "screendump /workspace/screen.ppm"
```

Each call opens a fresh connection, does the `qmp_capabilities` handshake, sends
one command, and returns. That's fine for QMP/HMP, which are request/reply.

## 3. Hold a stateful console (serial login, long monitor session)

The agent's `bash` tool is one-shot per call, so it can't hold a socket open
across calls. Run a **detached bridge** once: it pumps everything the socket
emits into `console.log` and feeds lines from the `console.in` fifo back to the
socket.

```sh
# Start the bridge (detached) — do this once.
setsid qemu-console serve /workspace/serial.sock /workspace/con \
    < /dev/null > /workspace/con/serve.log 2>&1 &

# Then, across separate tool calls:
echo 'ls -la' > /workspace/con/console.in   # send a line to the guest
tail -n 40 /workspace/con/console.log       # read what the console emitted
```

`serve` creates `<dir>/console.log` and `<dir>/console.in` (a fifo). Send input
by writing lines to the fifo; read output by tailing the log.

## 4. Raw one-shot (no QMP handshake)

For a plain socket (e.g. `-monitor unix:…` HMP, or a serial socket) when you
just want to send text and see the immediate reply:

```sh
qemu-console send /workspace/monitor.sock "info status" --wait 3
```

## Notes

- Prefer the unix socket over TCP: container networking is slirp4netns
  (outbound NAT), so reaching a host `localhost:port` is awkward; the socket on
  the mount is direct and needs no network.
- Clean up sockets between runs (`rm /workspace/*.sock`) — QEMU won't bind a
  socket path that already exists unless told to.
- `screendump` writes a PPM to a workspace path, which the agent can then read
  or `send_file` to the user.
