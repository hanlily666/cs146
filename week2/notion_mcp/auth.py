"""One-time Notion OAuth authorization-code flow.

    cd week2 && poetry run python -m notion_mcp.auth

This is the only place a browser is ever opened. It starts a local callback
server, sends the user to Notion's consent page, checks `state`, exchanges the
code for tokens, and writes them to the TokenStore. The MCP server only reads
and refreshes that file.
"""

import http.server
import logging
import os
import secrets
import sys
import time
import urllib.parse
import webbrowser

import httpx

from notion_mcp.tokens import (
    TOKEN_URL,
    AuthError,
    TokenStore,
    client_credentials,
    tokens_from_response,
)

log = logging.getLogger(__name__)

AUTHORIZE_URL = "https://api.notion.com/v1/oauth/authorize"
DEFAULT_REDIRECT_URI = "http://localhost:8765/callback"
CALLBACK_TIMEOUT_SECONDS = 300


def wait_for_callback(redirect_uri: str, expected_state: str) -> str:
    """Serve one request on the redirect URI and return the authorization code."""
    parsed = urllib.parse.urlparse(redirect_uri)
    result: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            url = urllib.parse.urlparse(self.path)
            if url.path != parsed.path:
                self.send_error(404)
                return
            params = dict(urllib.parse.parse_qsl(url.query))
            if not secrets.compare_digest(params.get("state", ""), expected_state):
                result["error"] = "state mismatch (possible CSRF); start the flow again"
            elif "error" in params:
                result["error"] = f"Notion returned error={params['error']}"
            elif "code" not in params:
                result["error"] = "callback had no code"
            else:
                result["code"] = params["code"]
            ok = "code" in result
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            msg = "Notion connected. You can close this tab." if ok else result["error"]
            self.wfile.write(msg.encode())

        def log_message(self, format, *args):  # keep the terminal quiet
            pass

    class Server(http.server.HTTPServer):
        def handle_timeout(self):
            result["error"] = f"no callback within {CALLBACK_TIMEOUT_SECONDS}s"

    server = Server((parsed.hostname or "localhost", parsed.port or 80), Handler)
    deadline = time.monotonic() + CALLBACK_TIMEOUT_SECONDS
    with server:
        # Loop because the browser may also request e.g. /favicon.ico.
        while not result:
            server.timeout = max(deadline - time.monotonic(), 0.1)
            server.handle_request()
    if "error" in result:
        raise AuthError(result["error"])
    return result["code"]


def exchange_code(code: str, redirect_uri: str) -> dict:
    resp = httpx.post(
        TOKEN_URL,
        json={"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri},
        auth=client_credentials(),
        timeout=30,
    )
    if resp.status_code != 200:
        raise AuthError(f"code exchange failed ({resp.status_code}): {resp.text[:300]}")
    return resp.json()


def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(message)s")
    try:
        client_id, _ = client_credentials()
        redirect_uri = os.environ.get("NOTION_REDIRECT_URI", DEFAULT_REDIRECT_URI)
        state = secrets.token_urlsafe(32)
        url = (
            AUTHORIZE_URL
            + "?"
            + urllib.parse.urlencode(
                {
                    "client_id": client_id,
                    "response_type": "code",
                    "owner": "user",
                    "redirect_uri": redirect_uri,
                    "state": state,
                }
            )
        )
        log.info("Opening Notion in your browser. If it doesn't open, visit:\n%s", url)
        webbrowser.open(url)
        code = wait_for_callback(redirect_uri, state)
        tokens = tokens_from_response(exchange_code(code, redirect_uri))
        store = TokenStore()
        store.save(tokens)
    except AuthError as e:
        log.error("Authorization failed: %s", e)
        sys.exit(1)
    log.info("Saved tokens for workspace %r to %s", tokens.workspace_name, store.path)


if __name__ == "__main__":
    main()
