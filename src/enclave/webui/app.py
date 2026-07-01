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

    # CORS — the API authenticates via Bearer tokens in the Authorization
    # header (never cookies), so credentials mode is unnecessary. Keeping
    # allow_credentials=True together with allow_origins=["*"] would let any
    # origin make credentialed cross-origin requests (security review L2).
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
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
    from enclave.webui.routes import asks, bugs, chat, fusion, memories, notifications, openspec, panel, schedules, sessions

    app.include_router(
        sessions.router,
        prefix="/api/sessions",
        tags=["sessions"],
        dependencies=[Depends(get_current_user)],
    )
    app.include_router(
        openspec.router,
        prefix="/api/sessions",
        tags=["openspec"],
        dependencies=[Depends(get_current_user)],
    )
    # Artifact file download accepts ?token= query param (browser links can't send headers)
    app.include_router(
        sessions.public_router,
        prefix="/api/sessions",
        tags=["sessions"],
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
        panel.router,
        prefix="/api/panel",
        tags=["panel"],
        dependencies=[Depends(get_current_user)],
    )
    app.include_router(
        fusion.router,
        prefix="/api/fusion",
        tags=["fusion"],
        dependencies=[Depends(get_current_user)],
    )
    app.include_router(
        schedules.router,
        prefix="/api/schedules",
        tags=["schedules"],
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
    # File/media proxy routes accept ?token= query param (browser <img> can't send headers)
    app.include_router(
        chat.public_router,
        prefix="/api/chat",
        tags=["chat"],
    )
    app.include_router(
        asks.router,
        prefix="/api",
        dependencies=[Depends(get_current_user)],
    )
    app.include_router(
        notifications.router,
        prefix="/api",
        dependencies=[Depends(get_current_user)],
    )
    # WebSocket route handles auth via query param (OAuth2 deps don't work with WS)
    app.include_router(
        notifications.ws_router,
        prefix="/api",
        tags=["notifications"],
    )

    # Health check (unprotected)
    @app.get("/api/health")
    async def health():
        return {"status": "ok", "auth_required": user_count() > 0}

    # Durable event persistence now happens at the SOURCE, inside the
    # orchestrator's control socket (_emit), so it is exactly-once and does
    # not depend on this process being subscribed. The old webui-side
    # EventPersister was lossy (any event emitted during a subscribe/reconnect
    # gap was dropped) and is intentionally no longer started here. The webui
    # only READS events.db for history.

    # Serve Vue SPA static files (built output) — must be last
    frontend_dist = Path(__file__).parent / "frontend" / "dist"
    if frontend_dist.exists():
        from fastapi.responses import FileResponse, HTMLResponse

        app.mount("/assets", StaticFiles(directory=str(frontend_dist / "assets")), name="assets")

        dist_root = frontend_dist.resolve()

        @app.get("/{full_path:path}")
        async def spa_fallback(full_path: str):
            """Serve index.html for all non-API routes (SPA client-side routing)."""
            # Containment guard: resolve the requested path and confirm it stays
            # within the dist root before serving. Without this, '..' / '%2e%2e'
            # segments (which Starlette/uvicorn do NOT normalize) would let an
            # unauthenticated caller read arbitrary files (TLS key, JWT signing
            # key, bcrypt hashes). On any escape or non-file, fall through to
            # index.html so client-side routing still works.
            candidate = (frontend_dist / full_path).resolve()
            try:
                candidate.relative_to(dist_root)
                is_contained = True
            except ValueError:
                is_contained = False
            if is_contained and candidate.is_file():
                return FileResponse(candidate)
            # Serve index.html with no-cache so browsers always get the
            # latest JS bundle references (asset files use content hashes).
            html = (frontend_dist / "index.html").read_text()
            return HTMLResponse(
                html,
                headers={
                    "Cache-Control": "no-cache, no-store, must-revalidate",
                    "Pragma": "no-cache",
                    "Expires": "0",
                },
            )

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
        "--ssl-certfile",
        type=str,
        default="/data/Enclave/tls/server.crt",
        help="Path to TLS certificate (default: /data/Enclave/tls/server.crt)",
    )
    parser.add_argument(
        "--ssl-keyfile",
        type=str,
        default="/data/Enclave/tls/server.key",
        help="Path to TLS private key (default: /data/Enclave/tls/server.key)",
    )
    parser.add_argument(
        "--no-tls",
        action="store_true",
        help="Disable TLS (serve over plain HTTP)",
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

    # TLS configuration
    ssl_kwargs = {}
    if not args.no_tls:
        certfile = Path(args.ssl_certfile)
        keyfile = Path(args.ssl_keyfile)
        if certfile.exists() and keyfile.exists():
            ssl_kwargs["ssl_certfile"] = str(certfile)
            ssl_kwargs["ssl_keyfile"] = str(keyfile)
            scheme = "https"
        else:
            print(f"[webui] WARNING: TLS cert/key not found at {certfile} / {keyfile} — falling back to HTTP",
                  file=sys.stderr)
            scheme = "http"
    else:
        scheme = "http"

    print(f"[webui] Starting Enclave Web UI on {scheme}://{args.host}:{args.port}", file=sys.stderr)
    if user_count() == 0:
        print("[webui] WARNING: No users configured — run with --create-user to add one",
              file=sys.stderr)

    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level="info",
        **ssl_kwargs,
    )


if __name__ == "__main__":
    main()
