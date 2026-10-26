"""OAuth token cache for the Notion integration.

Tokens live outside the repo at ~/.config/notion-mcp/token.json (override with
NOTION_MCP_TOKEN_FILE), mode 600, and are written atomically because Notion
rotates the refresh token on every refresh: a torn write would lose the only
valid refresh token.
"""

import logging
import os
import tempfile
from pathlib import Path

import httpx
from pydantic import BaseModel, ValidationError

log = logging.getLogger(__name__)

TOKEN_URL = "https://api.notion.com/v1/oauth/token"
DEFAULT_TOKEN_PATH = Path.home() / ".config" / "notion-mcp" / "token.json"
AUTH_COMMAND = "cd week2 && poetry run python -m notion_mcp.auth"


class Tokens(BaseModel):
    access_token: str
    refresh_token: str | None = None
    workspace_name: str | None = None
    bot_id: str


class AuthError(Exception):
    """Credentials are missing or can no longer be refreshed. Never retryable:
    the user has to re-run the auth command."""

    hint = f"Ask the user to run `{AUTH_COMMAND}` to re-authorize Notion, then retry."


def client_credentials() -> tuple[str, str]:
    client_id = os.environ.get("NOTION_CLIENT_ID")
    client_secret = os.environ.get("NOTION_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise AuthError("NOTION_CLIENT_ID and NOTION_CLIENT_SECRET must be set in the environment")
    return client_id, client_secret


def tokens_from_response(data: dict, previous: Tokens | None = None) -> Tokens:
    """Build Tokens from a /v1/oauth/token response, keeping fields the response omits."""
    if "access_token" not in data:
        raise AuthError("Notion's token response had no access_token")
    return Tokens(
        access_token=data["access_token"],
        refresh_token=data.get("refresh_token") or (previous and previous.refresh_token),
        workspace_name=data.get("workspace_name") or (previous and previous.workspace_name),
        bot_id=data.get("bot_id") or (previous.bot_id if previous else ""),
    )


class TokenStore:
    def __init__(self, path: Path | None = None):
        env_path = os.environ.get("NOTION_MCP_TOKEN_FILE")
        self.path = path or (Path(env_path).expanduser() if env_path else DEFAULT_TOKEN_PATH)

    def load(self) -> Tokens | None:
        try:
            return Tokens.model_validate_json(self.path.read_text())
        except FileNotFoundError:
            return None
        except ValidationError as e:
            raise AuthError(f"token file {self.path} is corrupt") from e

    def save(self, tokens: Tokens) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # mkstemp creates the file with mode 600, so the token is never world-readable.
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".token-", suffix=".json")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(tokens.model_dump_json(indent=2))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    async def refresh(self, client: httpx.AsyncClient | None = None) -> Tokens:
        """Exchange the cached refresh token for a new pair and save it.

        Raises AuthError when the grant is rejected (re-auth needed). Transient
        failures (network, 429, 5xx) raise httpx.TransportError / HTTPStatusError
        so the caller can report them as retryable.
        """
        current = self.load()
        if current is None:
            raise AuthError(f"no token file at {self.path}")
        if not current.refresh_token:
            raise AuthError("cached token has no refresh_token")
        payload = {"grant_type": "refresh_token", "refresh_token": current.refresh_token}

        owns_client = client is None
        client = client or httpx.AsyncClient(timeout=30)
        try:
            resp = await client.post(TOKEN_URL, json=payload, auth=client_credentials())
        finally:
            if owns_client:
                await client.aclose()

        if resp.status_code == 429 or resp.status_code >= 500:
            resp.raise_for_status()
        if resp.status_code != 200:
            try:
                code = resp.json().get("error", resp.status_code)
            except ValueError:
                code = resp.status_code
            raise AuthError(f"token refresh rejected by Notion ({code})")

        tokens = tokens_from_response(resp.json(), previous=current)
        self.save(tokens)
        log.info("refreshed Notion access token for workspace %r", tokens.workspace_name)
        return tokens
