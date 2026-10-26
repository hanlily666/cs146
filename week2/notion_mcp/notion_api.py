"""Thin async wrapper over the 7 Notion REST endpoints the tools need.

Every call goes through `_request`, which:
- adds the bearer token and Notion-Version header,
- spaces requests to stay under Notion's 3 requests/s average,
- on a 401 refreshes the token once (TokenStore.refresh) and retries once,
- turns any failure into NotionError (or AuthError when re-auth is needed).

Methods return Notion's JSON as-is; shaping for the agent happens in server.py.
"""

import asyncio
import logging
import math
import time
from urllib.parse import quote

import httpx

from notion_mcp.tokens import AuthError, Tokens, TokenStore

log = logging.getLogger(__name__)

BASE_URL = "https://api.notion.com/v1"
NOTION_VERSION = "2026-03-11"
MIN_REQUEST_INTERVAL = 1 / 3  # Notion's limit is an average of 3 requests/s


class NotionError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: str | None = None,
        retryable: bool = False,
        retry_after_seconds: int | None = None,
        request_id: str | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds
        self.request_id = request_id


def _error_from_response(resp: httpx.Response) -> NotionError:
    try:
        body = resp.json()
    except ValueError:
        body = {}
    status = resp.status_code
    retry_after = None
    if status == 429:
        try:
            retry_after = math.ceil(float(resp.headers["Retry-After"]))
        except (KeyError, ValueError):
            retry_after = 1
    return NotionError(
        body.get("message") or resp.reason_phrase or f"HTTP {status}",
        status=status,
        code=body.get("code"),
        # 409 conflict_error and 5xx/529 are transient per Notion's docs.
        retryable=status in (409, 429) or status >= 500,
        retry_after_seconds=retry_after,
        request_id=body.get("request_id"),
    )


def _id(notion_id: str) -> str:
    # IDs come from the agent; never let one change the URL path.
    return quote(notion_id.strip(), safe="")


def _text_content(text: str) -> dict:
    # Body of a text block (paragraph, heading, ...) holding unformatted text.
    return {"rich_text": [{"type": "text", "text": {"content": text}}]}


class NotionAPI:
    def __init__(self, store: TokenStore | None = None, client: httpx.AsyncClient | None = None):
        self.store = store or TokenStore()
        self._client = client or httpx.AsyncClient(base_url=BASE_URL, timeout=30)
        self._refresh_lock = asyncio.Lock()
        self._throttle_lock = asyncio.Lock()
        self._next_request_at = 0.0

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _throttle(self) -> None:
        async with self._throttle_lock:
            now = time.monotonic()
            wait = self._next_request_at - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._next_request_at = max(now, self._next_request_at) + MIN_REQUEST_INTERVAL

    async def _send(self, method: str, path: str, token: str, **kwargs) -> httpx.Response:
        await self._throttle()
        headers = {"Authorization": f"Bearer {token}", "Notion-Version": NOTION_VERSION}
        try:
            return await self._client.request(method, path, headers=headers, **kwargs)
        except httpx.TransportError as e:
            raise NotionError(f"network error talking to Notion: {e!r}", retryable=True) from e

    async def _refreshed_tokens(self, rejected_access_token: str) -> Tokens:
        async with self._refresh_lock:
            # Another call may have refreshed while we waited; refreshing again would
            # spend the (rotated) refresh token a second time.
            current = self.store.load()
            if current and current.access_token != rejected_access_token:
                return current
            try:
                return await self.store.refresh(self._client)
            except httpx.TransportError as e:
                raise NotionError(f"network error refreshing token: {e!r}", retryable=True) from e
            except httpx.HTTPStatusError as e:
                raise _error_from_response(e.response) from e

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        tokens = self.store.load()
        if tokens is None:
            raise AuthError(f"not authorized yet: no token file at {self.store.path}")

        resp = await self._send(method, path, tokens.access_token, **kwargs)
        if resp.status_code == 401:
            log.info("Notion returned 401; refreshing access token")
            tokens = await self._refreshed_tokens(tokens.access_token)
            resp = await self._send(method, path, tokens.access_token, **kwargs)
            if resp.status_code == 401:
                raise AuthError("Notion rejected the freshly refreshed token")

        if resp.is_success:
            return resp.json()
        err = _error_from_response(resp)
        log.info(
            "Notion %s %s -> %s %s (request_id=%s)",
            method,
            path,
            err.status,
            err.code,
            err.request_id,
        )
        raise err

    async def search_pages(self, query: str, limit: int) -> list[dict]:
        body = {
            "query": query,
            "filter": {"property": "object", "value": "page"},
            "page_size": limit,
        }
        return (await self._request("POST", "/search", json=body))["results"]

    async def get_block(self, block_id: str) -> dict:
        return await self._request("GET", f"/blocks/{_id(block_id)}")

    async def list_children(self, block_id: str, cursor: str | None = None) -> dict:
        params = {"page_size": 100}
        if cursor:
            params["start_cursor"] = cursor
        return await self._request("GET", f"/blocks/{_id(block_id)}/children", params=params)

    async def update_block_text(self, block_id: str, block_type: str, text: str) -> dict:
        body = {block_type: _text_content(text)}
        return await self._request("PATCH", f"/blocks/{_id(block_id)}", json=body)

    async def append_text_blocks(
        self, parent_id: str, block_type: str, texts: list[str], after_block_id: str | None = None
    ) -> list[str]:
        """Insert one plain-text block per item in texts; return the new block IDs."""
        children = [
            {"object": "block", "type": block_type, block_type: _text_content(t)} for t in texts
        ]
        body: dict = {"children": children}
        if after_block_id:
            body["position"] = {"type": "after_block", "after_block": {"id": after_block_id}}
        resp = await self._request("PATCH", f"/blocks/{_id(parent_id)}/children", json=body)
        # With `after`, Notion returns the new blocks followed by every later sibling.
        return [b["id"] for b in resp.get("results", [])[: len(texts)]]

    async def delete_block(self, block_id: str) -> dict:
        return await self._request("DELETE", f"/blocks/{_id(block_id)}")
