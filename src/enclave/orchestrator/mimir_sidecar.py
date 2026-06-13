"""Mimir Copilot sidecar — a warm-session LLM daemon for the librarian.

The Mimir librarian classifies each draft by invoking an LLM. Its default
Copilot adapter (``CopilotCliInvoker`` in the vendored Mimir crate) cold-spawns
a full ``copilot -p`` agent per draft: every call pays the CLI's startup cost
(JS bundle extract, MCP load, auth handshake) and runs a tool-enabled agentic
turn for what is really a single constrained text-transform. Measured ~37s per
draft, almost entirely overhead.

This sidecar keeps **one** ``copilot --acp`` server warm (via the same
``github-copilot-sdk`` Enclave already uses for agents) and answers many draft
classifications against it. The expensive cold start is paid once at boot; each
draft then costs only a cheap per-draft session plus model inference (~1-3s in
practice). Tools are disabled and the model is pinned, since the task needs
neither agentic tools nor an expensive model.

Protocol (newline-delimited JSON over a unix socket):

    request:  {"system_prompt": str, "user_message": str, "timeout"?: float}
    response: {"ok": true, "content": str}
              {"ok": false, "error": str}

The Rust ``SidecarInvoker`` connects, writes one request line, reads one
response line, and falls back to cold-spawn if the socket is unavailable, so the
sidecar is a pure optimization: the librarian still works without it.

Each draft is classified in a **fresh** per-draft session created against the
warm server, so one draft's context never leaks into the next and context never
grows unbounded. Requests are serialized with a lock: there is a single warm
server and the librarian is single-threaded, so concurrency would only add
contention.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
from pathlib import Path
from typing import Any

log = logging.getLogger("enclave.mimir.sidecar")

#: Default model for draft classification. Cheap + fast; the task is a
#: constrained prose -> Lisp transform, not reasoning-heavy.
DEFAULT_MODEL = "claude-haiku-4.5"

#: Per-request wall-clock cap for the model turn (seconds).
DEFAULT_TIMEOUT = 120.0


class MimirSidecar:
    """Holds one warm Copilot ACP client and classifies drafts against it."""

    def __init__(self, model: str = DEFAULT_MODEL, *, timeout: float = DEFAULT_TIMEOUT):
        self._model = model
        self._timeout = timeout
        self._client: Any | None = None
        self._lock = asyncio.Lock()  # serialize work against the single server
        self._starting = asyncio.Lock()  # guard concurrent (re)starts
        self._requests = 0

    # ── client lifecycle ────────────────────────────────────────────────
    async def _ensure_client(self) -> Any:
        """Return a live CopilotClient, (re)starting the warm server if needed."""
        if self._client is not None:
            return self._client
        async with self._starting:
            if self._client is not None:
                return self._client
            # Imported lazily so the module imports even where the SDK is absent
            # (e.g. a host without copilot installed) — start() is where it's
            # actually needed.
            from copilot import CopilotClient

            client = CopilotClient()
            await client.start()
            self._client = client
            log.info("Mimir sidecar: warm copilot --acp server started (model=%s)", self._model)
            return client

    async def _reset_client(self) -> None:
        """Tear down a dead client so the next request restarts it."""
        client, self._client = self._client, None
        if client is not None:
            try:
                await client.stop()
            except Exception:
                pass

    # ── classification ──────────────────────────────────────────────────
    async def classify(self, system_prompt: str, user_message: str, *, timeout: float | None = None) -> str:
        """Run one draft classification against the warm server.

        Creates a fresh per-draft session (isolation), sends the combined
        prompt, waits for the assistant's final message, and tears the session
        down. Retries once on a connection-level failure (the warm server may
        have died); a second failure propagates to the caller, which surfaces it
        as an error response so the librarian can fall back to cold-spawn.
        """
        async with self._lock:
            try:
                return await self._classify_once(system_prompt, user_message, timeout)
            except (ConnectionError, OSError, EOFError) as first:
                log.warning("Mimir sidecar: classify failed (%s); restarting server and retrying", first)
                await self._reset_client()
                return await self._classify_once(system_prompt, user_message, timeout)

    async def _classify_once(self, system_prompt: str, user_message: str, timeout: float | None) -> str:
        from copilot.generated.session_events import AssistantMessageData

        client = await self._ensure_client()

        async def _deny(_req: Any) -> Any:
            # Tools are disabled, so no permission request should ever arrive.
            # Return None defensively rather than approving anything.
            return None

        # Set the librarian's system prompt as the session's system message
        # rather than prepending it to every user turn. This is both faster and
        # more correct than the Rust cold-spawn adapter (which can only jam the
        # system prompt into the single `-p` arg because the CLI exposes no
        # system-prompt flag): the prompt is processed as a stable system prompt,
        # so the model's prefix cache hits across drafts and only the draft text
        # is new input each call. Fresh session per draft preserves isolation;
        # the identical system content still caches across sessions.
        create_kwargs: dict[str, Any] = {
            "on_permission_request": _deny,
            "model": self._model,
            "available_tools": [],  # pure transform — no tools, no agentic loop
        }
        if system_prompt:
            create_kwargs["system_message"] = {"mode": "replace", "content": system_prompt}

        session = await client.create_session(**create_kwargs)
        sid = getattr(session, "session_id", None)
        try:
            resp = await session.send_and_wait(user_message, timeout=timeout or self._timeout)
            self._requests += 1
            if resp is not None and isinstance(resp.data, AssistantMessageData):
                return resp.data.content or ""
            return ""
        finally:
            try:
                if sid is not None:
                    await client.delete_session(sid)
            except Exception:
                pass

    async def stop(self) -> None:
        await self._reset_client()


# ── socket server ───────────────────────────────────────────────────────
async def _handle_client(
    sidecar: MimirSidecar,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    """Serve newline-delimited JSON requests on one connection."""
    try:
        line = await reader.readline()
        if not line:
            return
        try:
            req = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            await _write(writer, {"ok": False, "error": f"bad request: {e}"})
            return
        system_prompt = str(req.get("system_prompt", ""))
        user_message = str(req.get("user_message", ""))
        timeout = req.get("timeout")
        if not user_message:
            await _write(writer, {"ok": False, "error": "missing user_message"})
            return
        try:
            content = await sidecar.classify(
                system_prompt, user_message,
                timeout=float(timeout) if timeout else None,
            )
            await _write(writer, {"ok": True, "content": content})
        except Exception as e:
            log.warning("Mimir sidecar: classify error: %s", e)
            await _write(writer, {"ok": False, "error": str(e)})
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


async def _write(writer: asyncio.StreamWriter, obj: dict) -> None:
    writer.write(json.dumps(obj).encode("utf-8") + b"\n")
    await writer.drain()


async def serve(socket_path: str, model: str, *, timeout: float = DEFAULT_TIMEOUT) -> None:
    """Run the sidecar server on a unix socket until cancelled."""
    sidecar = MimirSidecar(model=model, timeout=timeout)
    sock = Path(socket_path)
    sock.parent.mkdir(parents=True, exist_ok=True)
    if sock.exists():
        sock.unlink()

    server = await asyncio.start_unix_server(
        lambda r, w: _handle_client(sidecar, r, w), path=str(sock),
    )
    os.chmod(sock, 0o600)
    log.info("Mimir sidecar listening on %s", sock)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass

    async with server:
        await stop.wait()

    log.info("Mimir sidecar shutting down (served %d requests)", sidecar._requests)
    await sidecar.stop()
    try:
        sock.unlink()
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Mimir Copilot warm-session sidecar")
    parser.add_argument("--socket", required=True, help="Unix socket path to listen on")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Model for classification")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="Per-request timeout (s)")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        stream=sys.stderr,
    )
    try:
        asyncio.run(serve(args.socket, args.model, timeout=args.timeout))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
