#!/usr/bin/env python3
"""qemu_console — talk to a QEMU console/monitor exposed on a unix socket.

Designed for an Enclave agent (e.g. Brook) running inside a container to drive
a QEMU instance running on the host. QEMU is launched host-side with its
console(s) on unix sockets placed inside the session workspace, e.g.:

    qemu-system-x86_64 ... \
        -qmp    unix:/workspace/qmp.sock,server,nowait \
        -serial unix:/workspace/serial.sock,server,nowait

Because the workspace is a bidirectional bind mount, the container sees the
same socket inode at /workspace/*.sock and can connect to it.

This tool solves two problems:

  1. QMP handshake + one-shot commands (stateless per connect) — `qmp` / `hmp`.
  2. The agent's bash tool is one-shot per call, so a stateful console (a guest
     serial login, a long-running monitor session) can't be held open across
     calls. `serve` runs a detached bridge that pumps socket output into a log
     file and feeds a fifo's lines back to the socket, so the agent drives it
     across many calls via `echo cmd > console.in` + `tail console.log`.

Stdlib only — no dependencies, runs in any container.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import threading
import time


def _connect(path: str, timeout: float = 10.0) -> socket.socket:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect(path)
    return s


def _recv_until_idle(s: socket.socket, idle: float = 0.4, total: float = 5.0) -> bytes:
    """Read until the socket goes quiet for `idle` seconds or `total` elapses."""
    s.setblocking(False)
    buf = bytearray()
    start = time.monotonic()
    last = start
    while True:
        try:
            chunk = s.recv(65536)
            if chunk:
                buf.extend(chunk)
                last = time.monotonic()
            else:
                break  # peer closed
        except BlockingIOError:
            now = time.monotonic()
            if now - last >= idle or now - start >= total:
                break
            time.sleep(0.02)
        except Exception:
            break
    return bytes(buf)


def _read_json_line(s: socket.socket, timeout: float = 10.0) -> dict:
    """Read one newline-terminated JSON object (QMP is line-delimited JSON)."""
    s.settimeout(timeout)
    buf = bytearray()
    while b"\n" not in buf:
        chunk = s.recv(4096)
        if not chunk:
            break
        buf.extend(chunk)
    line = bytes(buf).split(b"\n", 1)[0]
    return json.loads(line.decode()) if line.strip() else {}


def _qmp_handshake(s: socket.socket) -> dict:
    """Consume the QMP greeting and negotiate capabilities."""
    greeting = _read_json_line(s)  # {"QMP": {...}}
    s.sendall(b'{"execute":"qmp_capabilities"}\n')
    _read_json_line(s)  # {"return": {}}
    return greeting


def cmd_qmp(args: argparse.Namespace) -> int:
    """Send a raw QMP command (JSON) and print the JSON reply."""
    s = _connect(args.socket)
    try:
        _qmp_handshake(s)
        # Accept either a full JSON object or just an execute-name.
        text = args.command.strip()
        if text.startswith("{"):
            payload = text
        else:
            payload = json.dumps({"execute": text})
        s.sendall(payload.encode() + b"\n")
        reply = _read_json_line(s, timeout=args.wait)
        print(json.dumps(reply, indent=2))
        return 0
    finally:
        s.close()


def cmd_hmp(args: argparse.Namespace) -> int:
    """Run a human-monitor (HMP) command via QMP's human-monitor-command."""
    s = _connect(args.socket)
    try:
        _qmp_handshake(s)
        payload = json.dumps({
            "execute": "human-monitor-command",
            "arguments": {"command-line": args.command},
        })
        s.sendall(payload.encode() + b"\n")
        reply = _read_json_line(s, timeout=args.wait)
        # human-monitor-command returns the console text in "return".
        out = reply.get("return", reply)
        print(out if isinstance(out, str) else json.dumps(reply, indent=2))
        return 0
    finally:
        s.close()


def cmd_send(args: argparse.Namespace) -> int:
    """Raw one-shot: send bytes to the socket, print whatever comes back."""
    s = _connect(args.socket)
    try:
        data = (args.text + ("\n" if not args.no_newline else "")).encode()
        s.sendall(data)
        out = _recv_until_idle(s, total=args.wait)
        sys.stdout.buffer.write(out)
        sys.stdout.flush()
        return 0
    finally:
        s.close()


def cmd_serve(args: argparse.Namespace) -> int:
    """Persistent bridge: socket -> log file, fifo -> socket.

    Run this detached (e.g. `nohup qemu_console.py serve ... &`). Then, across
    separate tool calls:
        echo 'ls -la' > <dir>/console.in     # send a line to the console
        tail -n 40 <dir>/console.log         # read what the console emitted
    """
    os.makedirs(args.dir, exist_ok=True)
    log_path = os.path.join(args.dir, "console.log")
    fifo_path = os.path.join(args.dir, "console.in")
    if not os.path.exists(fifo_path):
        os.mkfifo(fifo_path)

    s = _connect(args.socket, timeout=30.0)
    s.setblocking(True)
    stop = threading.Event()

    def reader() -> None:
        s.settimeout(1.0)
        with open(log_path, "ab", buffering=0) as log:
            while not stop.is_set():
                try:
                    chunk = s.recv(65536)
                    if not chunk:
                        log.write(b"\n[qemu_console: socket closed]\n")
                        stop.set()
                        break
                    log.write(chunk)
                except socket.timeout:
                    continue
                except Exception as e:
                    log.write(f"\n[qemu_console: reader error: {e}]\n".encode())
                    stop.set()
                    break

    t = threading.Thread(target=reader, daemon=True)
    t.start()

    # Writer: block-open the fifo repeatedly; each writer's lines go to the sock.
    try:
        while not stop.is_set():
            with open(fifo_path, "r") as fifo:
                for line in fifo:
                    if stop.is_set():
                        break
                    s.sendall(line.encode())
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        s.close()
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    q = sub.add_parser("qmp", help="send a raw QMP command (JSON or execute-name)")
    q.add_argument("socket")
    q.add_argument("command")
    q.add_argument("--wait", type=float, default=10.0)
    q.set_defaults(func=cmd_qmp)

    h = sub.add_parser("hmp", help="run a human-monitor command via QMP")
    h.add_argument("socket")
    h.add_argument("command")
    h.add_argument("--wait", type=float, default=10.0)
    h.set_defaults(func=cmd_hmp)

    se = sub.add_parser("send", help="raw one-shot send + read (serial/monitor)")
    se.add_argument("socket")
    se.add_argument("text")
    se.add_argument("--wait", type=float, default=3.0)
    se.add_argument("--no-newline", action="store_true")
    se.set_defaults(func=cmd_send)

    sv = sub.add_parser("serve", help="detached bridge: socket<->log/fifo")
    sv.add_argument("socket")
    sv.add_argument("dir")
    sv.set_defaults(func=cmd_serve)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
