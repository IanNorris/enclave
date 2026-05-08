"""FastAPI application factory and main entry point."""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

import uvicorn
from fastapi import APIRouter, Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.staticfiles import StaticFiles

from enclave.common.config import EnclaveConfig, load_config
from enclave.webui.auth import (
    create_token,
    create_user,
    get_current_user,
    user_count,
    verify_password,
)


def create_app(config: EnclaveConfig | None = None) -> FastAPI:
    """Create and configure the FastAPI application."""
    if config is None:
        config = load_config()

    app = FastAPI(
        title="Enclave Web UI",
        version="0.1.0",
        docs_url="/api/docs",
        redoc_url=None,
    )

    # Store config in app state for access in route handlers
    app.state.config = config

    # CORS — allow the webui port from any origin (needed when exposed off-localhost)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Auth router (unprotected)
    auth_router = APIRouter(prefix="/api/auth", tags=["auth"])

    @auth_router.post("/login")
    async def login(form_data: OAuth2PasswordRequestForm = Depends()):
        user = verify_password(form_data.username, form_data.password)
        if not user:
            raise HTTPException(status_code=401, detail="Invalid credentials")
        token = create_token(form_data.username)
        return {"access_token": token, "token_type": "bearer", "username": form_data.username}

    @auth_router.get("/me")
    async def me(user: dict = Depends(get_current_user)):
        return {"username": user["username"], "is_admin": user.get("is_admin", False)}

    app.include_router(auth_router)

    # Protected API routers — require valid JWT
    from enclave.webui.routes import bugs, chat, memories, sessions

    app.include_router(
        sessions.router,
        prefix="/api/sessions",
        tags=["sessions"],
        dependencies=[Depends(get_current_user)],
    )
    app.include_router(
        bugs.router,
        prefix="/api/bugs",
        tags=["bugs"],
        dependencies=[Depends(get_current_user)],
    )
    # Attachment download accepts ?token= query param (browser <img>/<a> can't send headers)
    app.include_router(
        bugs.public_router,
        prefix="/api/bugs",
        tags=["bugs"],
    )
    app.include_router(
        memories.router,
        prefix="/api/memories",
        tags=["memories"],
        dependencies=[Depends(get_current_user)],
    )
    app.include_router(
        chat.router,
        prefix="/api/chat",
        tags=["chat"],
        dependencies=[Depends(get_current_user)],
    )
    # WebSocket routes handle auth via query param (OAuth2 deps don't work with WS)
    app.include_router(
        chat.ws_router,
        prefix="/api/chat",
        tags=["chat"],
    )

    # Health check (unprotected)
    @app.get("/api/health")
    async def health():
        return {"status": "ok", "auth_required": user_count() > 0}

    # Background task: subscribe to control socket to cache agent responses
    @app.on_event("startup")
    async def _start_response_cacher():
        import asyncio
        from enclave.webui.routes.chat import _response_cache, _load_response_cache, _persist_response_cache

        data_dir = Path(config.data_dir)
        sock_path = data_dir / "control.sock"

        # Load persisted cache from disk
        _load_response_cache(data_dir)

        async def _cache_subscriber():
            """Persistent subscriber that caches all agent responses."""
            while True:
                if not sock_path.exists():
                    await asyncio.sleep(3.0)
                    continue
                try:
                    import json
                    reader, writer = await asyncio.open_unix_connection(str(sock_path))
                    # List sessions to subscribe to all
                    writer.write(json.dumps({"action": "list"}).encode() + b"\n")
                    await writer.drain()
                    line = await asyncio.wait_for(reader.readline(), timeout=5)
                    data = json.loads(line.decode())
                    writer.close()
                    await writer.wait_closed()

                    session_ids = [s["id"] for s in data.get("sessions", [])
                                   if s.get("status") == "running"]
                    if not session_ids:
                        await asyncio.sleep(10.0)
                        continue

                    # Subscribe to each running session
                    tasks = [
                        asyncio.create_task(_subscribe_session(sock_path, sid))
                        for sid in session_ids
                    ]
                    # Re-check session list every 60s
                    await asyncio.sleep(60.0)
                    for t in tasks:
                        t.cancel()

                except Exception:
                    await asyncio.sleep(5.0)

        async def _subscribe_session(sock_path: Path, session_id: str):
            import json
            from datetime import datetime, timezone
            try:
                reader, writer = await asyncio.open_unix_connection(str(sock_path))
                writer.write(json.dumps({
                    "action": "subscribe", "session": session_id
                }).encode() + b"\n")
                await writer.drain()

                while True:
                    line = await reader.readline()
                    if not line:
                        break
                    try:
                        event = json.loads(line.decode())
                    except json.JSONDecodeError:
                        continue
                    if event.get("type") == "response" and event.get("content"):
                        ts = datetime.now(timezone.utc).strftime(
                            "%Y-%m-%dT%H:%M:%S.%f"
                        )[:-3] + "Z"
                        cache = _response_cache.setdefault(session_id, [])
                        cache.append({
                            "role": "assistant",
                            "content": event["content"],
                            "timestamp": ts,
                        })
                        if len(cache) > 200:
                            _response_cache[session_id] = cache[-100:]
                        _persist_response_cache()
            except (OSError, ConnectionError, asyncio.CancelledError):
                pass
            finally:
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass

        asyncio.create_task(_cache_subscriber())

    # Serve Vue SPA static files (built output) — must be last
    frontend_dist = Path(__file__).parent / "frontend" / "dist"
    if frontend_dist.exists():
        from fastapi.responses import FileResponse

        app.mount("/assets", StaticFiles(directory=str(frontend_dist / "assets")), name="assets")

        @app.get("/{full_path:path}")
        async def spa_fallback(full_path: str):
            """Serve index.html for all non-API routes (SPA client-side routing)."""
            file_path = frontend_dist / full_path
            if file_path.is_file():
                return FileResponse(file_path)
            return FileResponse(frontend_dist / "index.html")

    return app


def main():
    """CLI entry point for the web UI server."""
    parser = argparse.ArgumentParser(description="Enclave Web UI Server")
    parser.add_argument(
        "--config", "-c",
        type=str,
        default=None,
        help="Path to enclave.yaml config file",
    )
    parser.add_argument(
        "--host",
        type=str,
        default="0.0.0.0",
        help="Bind address (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8430,
        help="Port to listen on (default: 8430)",
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help="Enable auto-reload for development",
    )
    parser.add_argument(
        "--create-user",
        metavar="USERNAME",
        help="Create or reset a user account, then exit",
    )
    parser.add_argument(
        "--admin",
        action="store_true",
        help="Grant admin privileges (use with --create-user)",
    )
    args = parser.parse_args()

    # User management mode
    if args.create_user:
        password = getpass.getpass(f"Password for '{args.create_user}': ")
        confirm = getpass.getpass("Confirm password: ")
        if password != confirm:
            print("Error: passwords do not match", file=sys.stderr)
            sys.exit(1)
        create_user(args.create_user, password, is_admin=args.admin)
        print(f"[webui] User '{args.create_user}' created/updated"
              f"{' (admin)' if args.admin else ''}")
        sys.exit(0)

    config = load_config(args.config)
    app = create_app(config)

    print(f"[webui] Starting Enclave Web UI on http://{args.host}:{args.port}", file=sys.stderr)
    if user_count() == 0:
        print("[webui] WARNING: No users configured — run with --create-user to add one",
              file=sys.stderr)

    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
