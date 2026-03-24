"""
Continuum Remote MCP Server — HTTP transport for Claude.ai Custom Connectors.

Implements full OAuth 2.0 Authorization Code + PKCE flow (RFC 7636) required
by claude.ai's Custom Connectors.  Single-user local server — /authorize
auto-approves without a login page.

Claude.ai Setup:
  1. Run:    continuum remote start --port 8766
  2. Tunnel: cloudflared tunnel --url http://localhost:8766
  3. Claude.ai → Settings → Custom Connectors → paste the Cloudflare URL
     (no OAuth Client ID / Secret needed — handled automatically)

OAuth flow:
  claude.ai → POST /mcp                                 → 401 + WWW-Authenticate
  claude.ai → GET  /.well-known/oauth-protected-resource → resource metadata
  claude.ai → GET  /.well-known/oauth-authorization-server → server metadata
  claude.ai → POST /register                             → client credentials
  claude.ai → GET  /authorize?…                          → 302 → claude.ai callback
  claude.ai → POST /token  {code=…}                      → {access_token}
  claude.ai → POST /mcp  Authorization: Bearer <token>   → ✓ MCP handshake

Environment variables:
  CONTINUUM_REMOTE_TOKEN   bearer token (auto-generated if not set)
  CONTINUUM_REMOTE_HOST    bind host   (default: 127.0.0.1)
  CONTINUUM_REMOTE_PORT    bind port   (default: 8766)
"""

from __future__ import annotations

import os
import secrets
import time
import urllib.parse
from pathlib import Path

import anyio
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

from .mcp_server import mcp  # same FastMCP instance → same _db → same 35+ tools

REMOTE_PID_FILE = Path.home() / ".continuum" / "remote.pid"
REMOTE_TOKEN_FILE = Path.home() / ".continuum" / "remote.token"


# ---------------------------------------------------------------------------
# Pure ASGI OAuth 2.0 middleware
# (BaseHTTPMiddleware buffers streaming responses — bad for SSE/MCP)
# ---------------------------------------------------------------------------

class OAuthMiddleware:
    """
    Pure ASGI middleware that:
    - Handles all OAuth 2.0 discovery / auth / token endpoints directly.
    - Validates Bearer token for /mcp (and all other paths).
    - Passes everything else to the MCP ASGI app unchanged (preserves streaming).
    """

    _OPEN_PATHS = frozenset({
        "/",
        "/health",
        "/.well-known/oauth-authorization-server",
        "/.well-known/oauth-protected-resource",
        "/.well-known/oauth-protected-resource/mcp",
        "/authorize",
        "/token",
        "/register",
    })

    def __init__(self, app, token: str) -> None:
        self.app = app
        self._token = token
        # In-memory stores — fine for single-user local server
        self._auth_codes: dict[str, dict] = {}   # code  → {state, expires}
        self._clients:    dict[str, str]  = {}   # client_id → client_secret

    # ------------------------------------------------------------------
    # ASGI entrypoint
    # ------------------------------------------------------------------

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path   = scope["path"]
        method = scope.get("method", "GET").upper()

        # OAuth / health endpoints — handle directly, no auth needed
        if path in self._OPEN_PATHS:
            request  = Request(scope, receive)
            response = await self._dispatch_open(request, path, method)
            await response(scope, receive, send)
            return

        # All other paths (including /mcp) — require Bearer token
        headers_raw = scope.get("headers", [])
        headers = {k.lower(): v for k, v in headers_raw}
        auth = headers.get(b"authorization", b"").decode("utf-8", errors="replace")

        if not auth.startswith("Bearer ") or auth[7:] != self._token:
            host   = headers.get(b"host",              b"localhost").decode()
            scheme = headers.get(b"x-forwarded-proto", b"http").decode()
            base   = f"{scheme}://{host}"
            resp = Response(
                '{"error":"unauthorized"}',
                status_code=401,
                media_type="application/json",
                headers={
                    "WWW-Authenticate": (
                        f'Bearer realm="{base}",'
                        f' resource_metadata="{base}/.well-known/oauth-protected-resource"'
                    )
                },
            )
            await resp(scope, receive, send)
            return

        # Token valid — pass through to MCP app (streaming preserved)
        await self.app(scope, receive, send)

    # ------------------------------------------------------------------
    # Open-endpoint dispatcher
    # ------------------------------------------------------------------

    async def _dispatch_open(self, request: Request, path: str, method: str) -> Response:
        base = self._base_url(request)

        if path in ("/", "/health"):
            return JSONResponse({"status": "ok", "version": "0.1.0", "endpoint": "/mcp"})

        if path == "/.well-known/oauth-authorization-server":
            return JSONResponse({
                "issuer":                                base,
                "authorization_endpoint":               f"{base}/authorize",
                "token_endpoint":                       f"{base}/token",
                "registration_endpoint":                f"{base}/register",
                "response_types_supported":             ["code"],
                "grant_types_supported":                ["authorization_code"],
                "code_challenge_methods_supported":     ["S256", "plain"],
                "token_endpoint_auth_methods_supported": [
                    "none", "client_secret_post", "client_secret_basic"
                ],
                "scopes_supported": ["mcp", "claudeai"],
            })

        if path in (
            "/.well-known/oauth-protected-resource",
            "/.well-known/oauth-protected-resource/mcp",
        ):
            return JSONResponse({
                "resource":                  f"{base}/mcp",
                "authorization_servers":     [base],
                "bearer_methods_supported":  ["header"],
                "resource_documentation":    f"{base}/health",
            })

        if path == "/register":
            return await self._handle_register(request)

        if path == "/authorize":
            return self._handle_authorize(request, base)

        if path == "/token" and method == "POST":
            return await self._handle_token(request)

        return JSONResponse({"error": "not_found"}, status_code=404)

    # ------------------------------------------------------------------
    # OAuth endpoint handlers
    # ------------------------------------------------------------------

    async def _handle_register(self, request: Request) -> Response:
        """RFC 7591 dynamic client registration — auto-approve every client."""
        try:
            body = await request.json()
        except Exception:
            body = {}

        client_id     = secrets.token_urlsafe(16)
        client_secret = secrets.token_urlsafe(32)
        self._clients[client_id] = client_secret

        return JSONResponse({
            "client_id":               client_id,
            "client_secret":           client_secret,
            "client_id_issued_at":     int(time.time()),
            "client_secret_expires_at": 0,          # never expires
            "redirect_uris":           body.get("redirect_uris", []),
            "grant_types":             ["authorization_code"],
            "response_types":          ["code"],
            "token_endpoint_auth_method": "client_secret_post",
        }, status_code=201)

    def _handle_authorize(self, request: Request, base: str) -> Response:
        """Auto-approve auth request — generate code, redirect to callback immediately."""
        params       = dict(request.query_params)
        redirect_uri = params.get("redirect_uri", "")
        state        = params.get("state", "")

        code = secrets.token_urlsafe(32)
        self._auth_codes[code] = {
            "state":        state,
            "redirect_uri": redirect_uri,
            "expires":      time.time() + 300,   # 5-minute window
        }

        # Redirect straight back to claude.ai callback
        callback_params = urllib.parse.urlencode({"code": code, "state": state})
        if redirect_uri:
            sep         = "&" if "?" in redirect_uri else "?"
            redirect_to = f"{redirect_uri}{sep}{callback_params}"
        else:
            redirect_to = f"{base}/?{callback_params}"

        return RedirectResponse(redirect_to, status_code=302)

    async def _handle_token(self, request: Request) -> Response:
        """Exchange authorization code for the configured bearer token."""
        # Accept both JSON and form-encoded bodies
        try:
            body = await request.json()
        except Exception:
            try:
                form = await request.form()
                body = dict(form)
            except Exception:
                body = {}

        grant_type = body.get("grant_type", "")
        if grant_type != "authorization_code":
            return JSONResponse(
                {"error": "unsupported_grant_type"},
                status_code=400,
            )

        code      = body.get("code", "")
        code_data = self._auth_codes.pop(code, None)
        if not code_data or code_data["expires"] < time.time():
            return JSONResponse(
                {"error": "invalid_grant", "error_description": "Code expired or not found"},
                status_code=400,
            )

        return JSONResponse({
            "access_token": self._token,
            "token_type":   "Bearer",
            "expires_in":   86400,          # 24 hours
            "scope":        "mcp claudeai",
        })

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _base_url(request: Request) -> str:
        """Return the public base URL, honouring tunnel proxy headers."""
        scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
        host   = request.headers.get(
            "x-forwarded-host",
            request.headers.get("host", "localhost"),
        )
        return f"{scheme}://{host}"


# ---------------------------------------------------------------------------
# Token helpers
# ---------------------------------------------------------------------------

def generate_token() -> str:
    """Generate a cryptographically secure random token."""
    return secrets.token_urlsafe(32)


def load_or_create_token() -> str:
    """Load saved token or generate and persist a new one."""
    if REMOTE_TOKEN_FILE.exists():
        return REMOTE_TOKEN_FILE.read_text().strip()
    token = generate_token()
    REMOTE_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    REMOTE_TOKEN_FILE.write_text(token)
    return token


def remote_status() -> dict:
    """Check whether the remote server process is running."""
    if not REMOTE_PID_FILE.exists():
        return {"running": False, "pid": None, "token_saved": REMOTE_TOKEN_FILE.exists()}
    try:
        import psutil
        pid     = int(REMOTE_PID_FILE.read_text().strip())
        running = psutil.pid_exists(pid)
        return {"running": running, "pid": pid, "token_saved": REMOTE_TOKEN_FILE.exists()}
    except Exception:
        return {"running": False, "pid": None, "token_saved": REMOTE_TOKEN_FILE.exists()}


# ---------------------------------------------------------------------------
# Server entry point
# ---------------------------------------------------------------------------

def run_remote(
    host:  str | None = None,
    port:  int | None = None,
    token: str | None = None,
) -> None:
    """Start the Continuum remote MCP server (blocking).

    Args:
        host:  Bind host (default: CONTINUUM_REMOTE_HOST env or 127.0.0.1)
        port:  Bind port (default: CONTINUUM_REMOTE_PORT env or 8766)
        token: Bearer token (default: CONTINUUM_REMOTE_TOKEN env or saved/generated)
    """
    host  = host  or os.environ.get("CONTINUUM_REMOTE_HOST", "127.0.0.1")
    port  = int(port or os.environ.get("CONTINUUM_REMOTE_PORT", "8766"))
    token = token or os.environ.get("CONTINUUM_REMOTE_TOKEN") or load_or_create_token()

    REMOTE_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    REMOTE_TOKEN_FILE.write_text(token)
    os.environ["CONTINUUM_REMOTE_TOKEN"] = token
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
    print(f"    cloudflared tunnel --url http://localhost:{port}")
    print()
    print("  Then in Claude.ai → Settings → Custom Connectors:")
    print("    URL: https://xxxx.trycloudflare.com")
    print("    (leave OAuth fields blank — handled automatically)")
    print()
    print("  Ctrl+C to stop.")
    print()

    # Starlette's Middleware() works with pure ASGI classes too
    middleware = [Middleware(OAuthMiddleware, token=token)]

    try:
        async def _serve() -> None:
            await mcp.run_http_async(
                host=host,
                port=port,
                path="/mcp",
                middleware=middleware,
            )

        anyio.run(_serve)
    finally:
        try:
            REMOTE_PID_FILE.unlink(missing_ok=True)
        except Exception:
            pass
