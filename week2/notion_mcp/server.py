"""Notion MCP server over stdio.

    cd week2 && poetry run python -m notion_mcp.server

Tools return small, shaped models (never raw Notion JSON). Failures are raised as
ToolError with a JSON body {error, retryable, retry_after_seconds?, hint} so the
agent can decide whether to retry.
"""

import json
import logging
import sys
import time
from collections.abc import AsyncIterator, Awaitable
from typing import Annotated, TypeVar

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.lifespan import lifespan
from pydantic import BaseModel, Field

from notion_mcp.notion_api import NotionAPI, NotionError
from notion_mcp.tokens import AuthError

log = logging.getLogger("notion_mcp")

T = TypeVar("T")

INSTRUCTIONS = (
    "Workflow: find_document -> [read_document, only when targeting specific blocks] -> "
    "preview_edit -> show the preview to the user -> commit_edit(preview_id). "
    "Appending to the end of a page needs only page_id."
)


# --- Output models ---------------------------------------------------------------------


class PageSummary(BaseModel):
    page_id: str
    title: str
    url: str
    last_edited_time: str


class BlockSummary(BaseModel):
    block_id: str
    type: str
    text: str
    editable: bool = Field(description="True if operation=replace can rewrite this block's text")
    has_children: bool = Field(description="Pass block_id to read_document to read nested blocks")


class PageContent(BaseModel):
    page_id: str
    title: str
    blocks: list[BlockSummary]
    next_cursor: str | None = Field(
        description="Pass as cursor to read_document to get the next blocks; null when done"
    )


# --- Shaping Notion JSON ---------------------------------------------------------------


def _plain_text(rich_text: list[dict]) -> str:
    return "".join(part.get("plain_text", "") for part in rich_text)


def _page_title(page: dict) -> str:
    # The title property's name varies ("title", "Name", ...); its type is always "title".
    for prop in page.get("properties", {}).values():
        if prop.get("type") == "title":
            return _plain_text(prop.get("title", []))
    return ""


def _block_text(block: dict) -> str:
    content = block.get(block["type"], {})
    if "rich_text" in content:
        return _plain_text(content["rich_text"])
    if "title" in content:  # child_page, child_database
        return content["title"]
    if "caption" in content:  # image, file, video, embed, ...
        return _plain_text(content["caption"])
    return ""


def _page_summary(page: dict) -> PageSummary:
    return PageSummary(
        page_id=page["id"],
        title=_page_title(page),
        url=page.get("url", ""),
        last_edited_time=page.get("last_edited_time", ""),
    )


def _block_summary(block: dict) -> BlockSummary:
    return BlockSummary(
        block_id=block["id"],
        type=block["type"],
        text=_block_text(block),
        # Any block whose content is rich_text (paragraphs, headings, list items, to_do,
        # quote, code, ...) can be rewritten by operation=replace.
        editable="rich_text" in block.get(block["type"], {}),
        has_children=block.get("has_children", False),
    )


# --- Error mapping ---------------------------------------------------------------------


def _hint(err: NotionError) -> str | None:
    if err.retryable:
        return None
    if err.status == 400:
        return "Fix the arguments and try again."
    if err.status == 403:
        return "The integration lacks permission for this page; the user must share it with the integration in Notion."
    if err.status == 404:
        return (
            "ID not found, or the page isn't shared with the integration. "
            "Get page IDs from find_document and block IDs from read_document."
        )
    return None


def _tool_error(err: Exception) -> ToolError:
    if isinstance(err, AuthError):
        body = {"error": str(err), "retryable": False, "hint": AuthError.hint}
    elif isinstance(err, NotionError):
        body = {"error": err.message, "retryable": err.retryable}
        if err.retry_after_seconds is not None:
            body["retry_after_seconds"] = err.retry_after_seconds
        if hint := _hint(err):
            body["hint"] = hint
    else:
        raise TypeError(f"unexpected error type {type(err).__name__}") from err
    return ToolError(json.dumps(body))


async def _call(tool: str, work: Awaitable[T]) -> T:
    """Run a tool body, log the outcome to stderr, and map API failures to ToolError."""
    start = time.monotonic()
    try:
        result = await work
    except (NotionError, AuthError) as e:
        log.info("%s failed in %.2fs: %s", tool, time.monotonic() - start, e)
        raise _tool_error(e) from e
    log.info("%s ok in %.2fs", tool, time.monotonic() - start)
    return result


# --- Server ----------------------------------------------------------------------------

_api: NotionAPI | None = None


def get_api() -> NotionAPI:
    global _api
    if _api is None:
        _api = NotionAPI()
    return _api


@lifespan
async def close_api_on_shutdown(_server: FastMCP) -> AsyncIterator[None]:
    try:
        yield
    finally:
        if _api is not None:
            await _api.aclose()


mcp = FastMCP("notion", instructions=INSTRUCTIONS, lifespan=close_api_on_shutdown)


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
async def find_document(
    query: Annotated[
        str, Field(min_length=1, description="Words from the page title, e.g. 'meeting notes'")
    ],
    limit: Annotated[int, Field(ge=1, le=25)] = 10,
) -> list[PageSummary]:
    """Search Notion pages shared with this integration by title.

    Returns page_ids to pass to read_document or preview_edit. Most recently
    edited pages come first. An empty list means no shared page matches.
    """

    async def work() -> list[PageSummary]:
        pages = await get_api().search_pages(query, limit)
        return [_page_summary(p) for p in pages]

    return await _call("find_document", work())


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
async def read_document(
    page_id: Annotated[
        str,
        Field(
            description="A page_id from find_document, or a block_id from read_document "
            "whose has_children is true"
        ),
    ],
    cursor: Annotated[
        str | None, Field(description="next_cursor from a previous read_document call")
    ] = None,
) -> PageContent:
    """Read a page's top-level blocks (up to 100 per call) with their block_ids.

    Use the block_ids in preview_edit to replace, delete, or append after a block.
    Nested blocks are not expanded: call read_document again with a block_id
    whose has_children is true.
    """

    async def work() -> PageContent:
        api = get_api()
        # A page is also a block (type child_page), so one call covers both kinds of ID.
        parent = await api.get_block(page_id)
        children = await api.list_children(page_id, cursor)
        return PageContent(
            page_id=parent["id"],
            title=_block_text(parent),
            blocks=[_block_summary(b) for b in children["results"]],
            next_cursor=children.get("next_cursor") if children.get("has_more") else None,
        )

    return await _call("read_document", work())


def main() -> None:
    # stdout carries the MCP protocol in stdio mode, so logs must go to stderr.
    logging.basicConfig(
        level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(name)s %(message)s"
    )
    # httpx logs every request URL at INFO; our own per-tool lines are enough.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    # The banner also triggers a PyPI update check on every start.
    mcp.run(show_banner=False)


if __name__ == "__main__":
    main()
