"""
Continuum Remote MCP Server — HTTP transport for Claude.ai Custom Connectors.

Exposes the same 35+ MCP tools as the local stdio server, pointed at the same
~/.continuum/continuum.db. Bearer token auth protects the endpoint.

Claude.ai Setup:
  1. Run: continuum remote start --port 8766 --token <yourtoken>
  2. Expose publicly: ngrok http 8766  (or Cloudflare Tunnel)
  3. Claude.ai → Settings → Custom Connectors → Add:
       URL:   https://your-tunnel-url/mcp
       Auth:  Bearer <yourtoken>

Environment variables:
  CONTINUUM_REMOTE_TOKEN   bearer token (auto-generated if not set)
  CONTINUUM_REMOTE_HOST    bind host (default: 127.0.0.1)
  CONTINUUM_REMOTE_PORT    bind port (default: 8766)
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

import anyio
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from .mcp_server import mcp  # same FastMCP instance → same _db → same tools

REMOTE_PID_FILE = Path.home() / ".continuum" / "remote.pid"
REMOTE_TOKEN_FILE = Path.home() / ".continuum" / "remote.token"


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Validate Authorization: Bearer <token> header on every request."""

    def __init__(self, app, token: str) -> None:
        super().__init__(app)
        self._token = token

    async def dispatch(self, request: Request, call_next):
        # Health check bypasses auth
        if request.url.path in ("/health", "/"):
            return await call_next(request)
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or auth[7:] != self._token:
            return Response(
                '{"error":"Unauthorized"}',
                status_code=401,
                media_type="application/json",
            )
        return await call_next(request)


def generate_token() -> str:
    """Generate a cryptographically secure random token."""
    return secrets.token_urlsafe(32)


def load_or_create_token() -> str:
    """Load saved token or generate and save a new one."""
    if REMOTE_TOKEN_FILE.exists():
        return REMOTE_TOKEN_FILE.read_text().strip()
    token = generate_token()
    REMOTE_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    REMOTE_TOKEN_FILE.write_text(token)
    return token


def remote_status() -> dict:
    """Check if the remote server is running."""
    if not REMOTE_PID_FILE.exists():
        return {"running": False, "pid": None, "token_saved": REMOTE_TOKEN_FILE.exists()}
    try:
        import psutil
        pid = int(REMOTE_PID_FILE.read_text().strip())
        running = psutil.pid_exists(pid)
        return {"running": running, "pid": pid, "token_saved": REMOTE_TOKEN_FILE.exists()}
    except Exception:
        return {"running": False, "pid": None, "token_saved": REMOTE_TOKEN_FILE.exists()}


def run_remote(
    host: str | None = None,
    port: int | None = None,
    token: str | None = None,
) -> None:
    """Start the Continuum remote MCP server (blocking).

    Args:
        host: Bind host (default: CONTINUUM_REMOTE_HOST env or 127.0.0.1)
        port: Bind port (default: CONTINUUM_REMOTE_PORT env or 8766)
        token: Bearer token (default: CONTINUUM_REMOTE_TOKEN env or saved/generated)
    """
    host = host or os.environ.get("CONTINUUM_REMOTE_HOST", "127.0.0.1")
    port = int(port or os.environ.get("CONTINUUM_REMOTE_PORT", "8766"))
    token = token or os.environ.get("CONTINUUM_REMOTE_TOKEN") or load_or_create_token()

    # Persist token so CLI can display it later
    REMOTE_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    REMOTE_TOKEN_FILE.write_text(token)
    os.environ["CONTINUUM_REMOTE_TOKEN"] = token

    # Write PID for status checks
    REMOTE_PID_FILE.write_text(str(os.getpid()))

    print()
    print("  ╔══════════════════════════════════════════════════╗")
    print("  ║       Continuum Remote MCP Server                ║")
    print("  ╚══════════════════════════════════════════════════╝")
    print()
    print(f"  Local URL : http://{host}:{port}/mcp")
    print(f"  Token     : {token}")
    print()
    print("  To expose to Claude.ai:")
    print(f"    ngrok http {port}")
    print("    → copy https://xxxx.ngrok.io/mcp into Claude.ai Custom Connectors")
    print("    → set Auth: Bearer <token above>")
    print()
    print("  Ctrl+C to stop.")
    print()

    middleware = [Middleware(BearerAuthMiddleware, token=token)]

    try:
        anyio.run(
            mcp.run_http_async,
            host=host,
            port=port,
            path="/mcp",
            middleware=middleware,
        )
    finally:
        # Clean up PID file on exit
        try:
            REMOTE_PID_FILE.unlink(missing_ok=True)
        except Exception:
            pass
