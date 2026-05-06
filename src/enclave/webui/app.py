"""FastAPI application factory and main entry point."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from enclave.common.config import EnclaveConfig, load_config


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

    # CORS — localhost only for now
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:8430", "http://127.0.0.1:8430"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Register API routers
    from enclave.webui.routes import sessions, bugs, memories

    app.include_router(sessions.router, prefix="/api/sessions", tags=["sessions"])
    app.include_router(bugs.router, prefix="/api/bugs", tags=["bugs"])
    app.include_router(memories.router, prefix="/api/memories", tags=["memories"])

    # Health check
    @app.get("/api/health")
    async def health():
        return {"status": "ok"}

    # Serve Vue SPA static files (built output) — must be last
    frontend_dist = Path(__file__).parent / "frontend" / "dist"
    if frontend_dist.exists():
        app.mount("/", StaticFiles(directory=str(frontend_dist), html=True), name="spa")

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
        default="127.0.0.1",
        help="Bind address (default: 127.0.0.1, localhost only)",
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
    args = parser.parse_args()

    config = load_config(args.config)
    app = create_app(config)

    print(f"[webui] Starting Enclave Web UI on http://{args.host}:{args.port}", file=sys.stderr)
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
