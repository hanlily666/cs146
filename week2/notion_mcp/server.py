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
import uuid
from collections.abc import AsyncIterator, Awaitable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Literal, TypeVar

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

PREVIEW_TTL_SECONDS = 600

COMMIT_RETRY_HINT = (
    "The preview is used up and this change may or may not have reached Notion. "
    "Call read_document to check, then preview_edit what is still missing."
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
    checked: bool | None = Field(default=None, description="to_do blocks only: whether it's ticked")


class PageContent(BaseModel):
    page_id: str
    title: str
    blocks: list[BlockSummary]
    next_cursor: str | None = Field(
        description="Pass as cursor to read_document to get the next blocks; null when done"
    )


# --- Edit models -----------------------------------------------------------------------

AppendBlockType = Literal[
    "paragraph",
    "heading_1",
    "heading_2",
    "heading_3",
    "bulleted_list_item",
    "numbered_list_item",
    "to_do",
    "quote",
]

# Notion caps a single rich_text item's content at 2000 characters.
Text = Annotated[str, Field(min_length=1, max_length=2000)]

BlockId = Annotated[str, Field(description="A block_id from read_document of this page")]


class ReplaceChange(BaseModel):
    """Rewrite one block's text in place. The block must have editable=true."""

    operation: Literal["replace"]
    block_id: BlockId
    text: Annotated[Text, Field(description="The block's new text, as plain text")]


class AppendChange(BaseModel):
    """Insert new blocks, one per item in texts, all of the same block_type."""

    operation: Literal["append"]
    texts: Annotated[
        list[Text], Field(min_length=1, max_length=100, description="One new block per item")
    ]
    block_type: AppendBlockType = "paragraph"
    after_block_id: Annotated[
        str | None,
        Field(
            description="Insert right after this block_id from read_document; "
            "null appends to the end of the page"
        ),
    ] = None


class DeleteChange(BaseModel):
    """Move one block (and its nested blocks) to Notion's trash."""

    operation: Literal["delete"]
    block_id: BlockId


Change = Annotated[ReplaceChange | AppendChange | DeleteChange, Field(discriminator="operation")]


class ChangePreview(BaseModel):
    operation: Literal["replace", "append", "delete"]
    block_id: str | None = Field(
        description="Block replaced or deleted; for append, the block inserted after "
        "(null = end of page)"
    )
    block_type: str
    before: str | None = Field(description="Current text; null for append")
    after: str | None = Field(description="New text, one line per new block; null for delete")


class EditPreview(BaseModel):
    preview_id: str = Field(description="Pass to commit_edit after the user approves")
    page_id: str
    page_title: str
    changes: list[ChangePreview]
    expires_at: str
    notes: list[str]


class ErrorBody(BaseModel):
    error: str
    retryable: bool
    retry_after_seconds: int | None = None
    hint: str | None = None


class EditResult(BaseModel):
    applied: int = Field(
        description="How many changes were applied, in preview order. If error is set, "
        "the change at this index failed and the ones after it were not sent."
    )
    total: int
    created_block_ids: list[str] = Field(description="IDs of the blocks added by append")
    error: ErrorBody | None = Field(
        default=None,
        description="Set when some changes were applied before one failed. If none were "
        "applied, commit_edit returns a tool error instead.",
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


def _has_formatting(rich_text: list[dict]) -> bool:
    for part in rich_text:
        if part.get("type") != "text" or part.get("href"):
            return True  # mentions, equations, links
        if any(v not in (False, "default") for v in part.get("annotations", {}).values()):
            return True  # bold, italic, code, color, ...
    return False


def _parent_id(block: dict) -> str:
    parent = block["parent"]
    return parent[parent["type"]]


def _editable(block: dict) -> bool:
    # Any block whose content is rich_text (paragraphs, headings, list items, to_do,
    # quote, code, ...) can be rewritten by operation=replace.
    return "rich_text" in block.get(block["type"], {})


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
        editable=_editable(block),
        has_children=block.get("has_children", False),
        checked=block["to_do"].get("checked") if block["type"] == "to_do" else None,
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


class InvalidRequest(Exception):
    """A request our own checks reject; retrying it unchanged cannot succeed."""

    def __init__(self, error: str, hint: str):
        super().__init__(error)
        self.hint = hint


def _error_body(err: Exception) -> dict:
    if isinstance(err, InvalidRequest):
        return {"error": str(err), "retryable": False, "hint": err.hint}
    if isinstance(err, AuthError):
        return {"error": str(err), "retryable": False, "hint": AuthError.hint}
    if isinstance(err, NotionError):
        body = {"error": err.message, "retryable": err.retryable}
        if err.retry_after_seconds is not None:
            body["retry_after_seconds"] = err.retry_after_seconds
        if hint := _hint(err):
            body["hint"] = hint
        return body
    raise TypeError(f"unexpected error type {type(err).__name__}") from err


def _tool_error(err: Exception) -> ToolError:
    return ToolError(json.dumps(_error_body(err)))


async def _call(tool: str, work: Awaitable[T]) -> T:
    """Run a tool body, log the outcome to stderr, and map failures to ToolError."""
    start = time.monotonic()
    try:
        result = await work
    except (NotionError, AuthError, InvalidRequest) as e:
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
        str,
        Field(
            description="Words from the page title, e.g. 'meeting notes'; omit to list "
            "the most recently edited pages"
        ),
    ] = "",
    limit: Annotated[int, Field(ge=1, le=25)] = 10,
) -> list[PageSummary]:
    """Search Notion pages shared with this integration by title.

    Returns page_ids to pass to read_document or preview_edit. Most recently
    edited pages come first, so with no query the first result is the page the
    user touched last. An empty list means no shared page matches.
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


# --- Preview store ---------------------------------------------------------------------


@dataclass
class _PlannedChange:
    change: ReplaceChange | AppendChange | DeleteChange
    block_type: str  # type of the targeted block (replace/delete) or of the new blocks (append)
    parent_id: str | None = None  # append only: where the new blocks go


@dataclass
class _PendingEdit:
    changes: list[_PlannedChange]
    expires_at: float  # time.time()
    used: bool = False


# In memory on purpose: previews are short-lived, and a restart only means previewing again.
_previews: dict[str, _PendingEdit] = {}


def _forget_old_previews(now: float) -> None:
    # Keep expired previews for one more TTL so commit_edit can say "expired"
    # instead of "unknown".
    for pid in [p for p, e in _previews.items() if now > e.expires_at + PREVIEW_TTL_SECONDS]:
        del _previews[pid]


def _take_preview(preview_id: str) -> _PendingEdit:
    """Claim a preview for commit. No await here, so two commits can't both claim it."""
    again = "Call preview_edit again and show the user the new preview."
    pending = _previews.get(preview_id)
    if pending is None:
        raise InvalidRequest(
            f"unknown preview_id {preview_id!r}: it was never issued, expired more than "
            f"{PREVIEW_TTL_SECONDS // 60} minutes ago, or the server restarted",
            again,
        )
    if pending.used:
        raise InvalidRequest(f"preview_id {preview_id!r} was already committed", again)
    if time.time() > pending.expires_at:
        raise InvalidRequest(
            f"preview_id {preview_id!r} expired (previews last {PREVIEW_TTL_SECONDS // 60} minutes)",
            again,
        )
    pending.used = True
    return pending


async def _plan(
    page_id: str, changes: list[Change]
) -> tuple[dict, list[_PlannedChange], list[ChangePreview], list[str]]:
    """Fetch the page and every targeted block, check each change, and build the preview.
    Only GET requests: nothing in Notion changes."""
    api = get_api()
    page = await api.get_block(page_id)
    blocks: dict[str, dict] = {}

    async def fetch(block_id: str) -> dict:
        if block_id not in blocks:
            blocks[block_id] = await api.get_block(block_id)
        return blocks[block_id]

    async def owning_page(block: dict) -> str | None:
        # Walk up through nested blocks to the page that holds them.
        while block["parent"]["type"] == "block_id":
            block = await fetch(block["parent"]["block_id"])
        return block["parent"].get("page_id")

    planned: list[_PlannedChange] = []
    previews: list[ChangePreview] = []
    notes: list[str] = []
    deleted: dict[str, int] = {}  # block["id"] as Notion returns it -> index of the delete
    for i, change in enumerate(changes):
        target_id = change.after_block_id if isinstance(change, AppendChange) else change.block_id
        block = None
        if target_id is not None:
            block = await fetch(target_id)
            if (j := deleted.get(block["id"])) is not None:
                raise InvalidRequest(
                    f"changes[{i}] targets block {target_id}, which changes[{j}] deletes",
                    "Drop one of the two changes.",
                )
            if block.get("in_trash") or block.get("archived"):
                raise InvalidRequest(
                    f"changes[{i}]: block {target_id} is already in the trash",
                    "Call read_document to get current block_ids.",
                )
            if block["id"] != page["id"] and (owner := await owning_page(block)) != page["id"]:
                raise InvalidRequest(
                    f"changes[{i}]: block {target_id} is on a different page ({owner}), "
                    f"not {page['id']}",
                    "Use block_ids from read_document of this page_id.",
                )

        # A change that writes inside a block an earlier change deletes (e.g. the
        # page itself) would land in the trash.
        container = _parent_id(block) if block else page["id"]
        if (j := deleted.get(container)) is not None:
            raise InvalidRequest(
                f"changes[{i}] edits inside block {container}, which changes[{j}] deletes",
                "Drop one of the two changes.",
            )

        if isinstance(change, (ReplaceChange, DeleteChange)):
            if isinstance(change, ReplaceChange):
                if not _editable(block):
                    raise InvalidRequest(
                        f"changes[{i}]: block {target_id} has type {block['type']}, which has "
                        "no text that replace can rewrite",
                        "Only blocks with editable=true in read_document can be replaced.",
                    )
                if _has_formatting(block[block["type"]]["rich_text"]):
                    notes.append(
                        f"changes[{i}]: replace writes plain text, so this block's inline "
                        "formatting (bold, links, mentions, ...) will be lost."
                    )
            else:
                if block["id"] == page["id"]:
                    notes.append(f"changes[{i}]: this deletes the page itself, not a block on it.")
                elif block["has_children"]:
                    notes.append(
                        f"changes[{i}]: deleting this block also deletes its nested blocks."
                    )
                deleted[block["id"]] = i
            planned.append(_PlannedChange(change, block["type"]))
            previews.append(
                ChangePreview(
                    operation=change.operation,
                    block_id=block["id"],
                    block_type=block["type"],
                    before=_block_text(block),
                    after=change.text if isinstance(change, ReplaceChange) else None,
                )
            )
        else:
            if block and block["id"] == page["id"]:
                raise InvalidRequest(
                    f"changes[{i}]: after_block_id {target_id} is the page itself, "
                    "not a block on it",
                    "Omit after_block_id to append at the end of the page.",
                )
            # Insert next to after_block_id, i.e. under its own parent, so this also
            # works for nested blocks.
            planned.append(_PlannedChange(change, change.block_type, container))
            previews.append(
                ChangePreview(
                    operation="append",
                    block_id=block["id"] if block else None,
                    block_type=change.block_type,
                    before=None,
                    after="\n".join(change.texts),
                )
            )

    if deleted:
        notes.append("Deleted blocks go to Notion's trash and can be restored from there.")
    return page, planned, previews, notes


async def _apply(planned: _PlannedChange) -> list[str]:
    """Send one change to Notion; return the IDs of any blocks it created."""
    api = get_api()
    change = planned.change
    if isinstance(change, ReplaceChange):
        await api.update_block_text(change.block_id, planned.block_type, change.text)
        return []
    if isinstance(change, DeleteChange):
        await api.delete_block(change.block_id)
        return []
    return await api.append_text_blocks(
        planned.parent_id, change.block_type, change.texts, change.after_block_id
    )


# --- Edit tools ------------------------------------------------------------------------


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
async def preview_edit(
    page_id: Annotated[str, Field(description="A page_id from find_document")],
    changes: Annotated[
        list[Change],
        Field(
            min_length=1,
            max_length=20,
            description="Applied in this order by commit_edit. Use operation=replace to "
            "rewrite a block, append to add blocks, delete to remove one.",
        ),
    ],
) -> EditPreview:
    """Check a set of edits to one page and show before/after text. Changes nothing in Notion.

    block_id and after_block_id values come from read_document of this page.
    Show the returned changes and notes to the user; once they approve, pass
    preview_id to commit_edit. The preview expires after 10 minutes.
    """

    async def work() -> EditPreview:
        page, planned, previews, notes = await _plan(page_id, changes)
        now = time.time()
        _forget_old_previews(now)
        preview_id = str(uuid.uuid4())
        expires_at = now + PREVIEW_TTL_SECONDS
        _previews[preview_id] = _PendingEdit(planned, expires_at)
        return EditPreview(
            preview_id=preview_id,
            page_id=page["id"],
            page_title=_block_text(page),
            changes=previews,
            expires_at=datetime.fromtimestamp(expires_at, UTC).isoformat(timespec="seconds"),
            notes=notes,
        )

    return await _call("preview_edit", work())


@mcp.tool(
    annotations={
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
async def commit_edit(
    preview_id: Annotated[
        str, Field(description="preview_id from preview_edit, after the user approved it")
    ],
) -> EditResult:
    """Apply a preview's changes to Notion, in order. Each preview_id works once.

    Notion has no transactions: if a change fails, the ones before it stay applied
    and the ones after it are not sent. The preview is used up either way, so to
    finish, call read_document and then preview_edit with the remaining changes.
    """

    async def work() -> EditResult:
        pending = _take_preview(preview_id)
        total = len(pending.changes)
        created: list[str] = []
        applied, error = 0, None
        for planned in pending.changes:
            try:
                created += await _apply(planned)
            except (NotionError, AuthError) as e:
                log.info("commit_edit stopped at change %d of %d", applied, total)
                error = ErrorBody(**_error_body(e))
                if error.retryable:
                    # A timeout may have reached Notion, and the preview is used up.
                    error.hint = COMMIT_RETRY_HINT
                if applied == 0:
                    raise ToolError(error.model_dump_json(exclude_none=True)) from e
                break
            applied += 1
        return EditResult(applied=applied, total=total, created_block_ids=created, error=error)

    return await _call("commit_edit", work())


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
