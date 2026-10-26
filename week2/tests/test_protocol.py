"""Protocol-level tests: every call goes through fastmcp.Client (in-memory MCP transport).

Only Notion's HTTP API is mocked (respx); the server code runs for real.

    cd week2 && poetry run python -m pytest tests
"""

import json

import httpx
import pytest
import respx
from fastmcp import Client

from notion_mcp import notion_api, server
from notion_mcp.notion_api import BASE_URL, NotionAPI
from notion_mcp.tokens import TOKEN_URL, Tokens, TokenStore

PAGE = "11111111-1111-1111-1111-111111111111"
HEADING = "22222222-2222-2222-2222-222222222222"
BULLET = "33333333-3333-3333-3333-333333333333"
NEW = "44444444-4444-4444-4444-444444444444"


def rich(text: str) -> list[dict]:
    return [{"type": "text", "plain_text": text, "text": {"content": text}, "annotations": {}}]


def page_block() -> dict:
    return {
        "object": "block",
        "id": PAGE,
        "type": "child_page",
        "child_page": {"title": "Scratch"},
        "parent": {"type": "workspace", "workspace": True},
        "has_children": True,
    }


def block(block_id: str, block_type: str, text: str) -> dict:
    return {
        "object": "block",
        "id": block_id,
        "type": block_type,
        block_type: {"rich_text": rich(text)},
        "parent": {"type": "page_id", "page_id": PAGE},
        "has_children": False,
        "in_trash": False,
    }


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("NOTION_CLIENT_ID", "test-client")
    monkeypatch.setenv("NOTION_CLIENT_SECRET", "test-secret")
    monkeypatch.setattr(notion_api, "MIN_REQUEST_INTERVAL", 0)
    store = TokenStore(tmp_path / "token.json")
    store.save(Tokens(access_token="old-access", refresh_token="old-refresh", bot_id="bot"))
    monkeypatch.setattr(server, "_api", NotionAPI(store=store))
    server._previews.clear()
    with respx.mock(assert_all_called=False) as mock:
        yield mock


def error_body(result) -> dict:
    assert result.is_error
    return json.loads(result.content[0].text)


@pytest.mark.anyio
async def test_tool_annotations():
    async with Client(server.mcp) as c:
        tools = {t.name: t for t in await c.list_tools()}
    assert set(tools) == {"find_document", "read_document", "preview_edit", "commit_edit"}
    for name in ("find_document", "read_document", "preview_edit"):
        assert tools[name].annotations.read_only_hint is True
    assert tools["commit_edit"].annotations.destructive_hint is True


@pytest.mark.anyio
async def test_find_then_read_returns_shaped_output(api):
    page = {
        "object": "page",
        "id": PAGE,
        "url": "https://notion.so/scratch",
        "last_edited_time": "2026-10-23T00:00:00.000Z",
        "properties": {"Name": {"type": "title", "title": rich("Scratch")}},
        "created_by": {"id": "someone"},  # dropped by shaping
    }
    api.post(f"{BASE_URL}/search").respond(200, json={"results": [page]})
    api.get(f"{BASE_URL}/blocks/{PAGE}").respond(200, json=page_block())
    api.get(f"{BASE_URL}/blocks/{PAGE}/children").respond(
        200, json={"results": [block(HEADING, "heading_4", "Ideas")], "has_more": False}
    )
    async with Client(server.mcp) as c:
        found = (await c.call_tool("find_document", {"query": "scratch"})).structured_content
        page_id = found["result"][0]["page_id"]
        read = (await c.call_tool("read_document", {"page_id": page_id})).structured_content
    assert set(found["result"][0]) == {"page_id", "title", "url", "last_edited_time"}
    assert read["title"] == "Scratch"
    assert read["blocks"] == [
        {
            "block_id": HEADING,
            "type": "heading_4",
            "text": "Ideas",
            "editable": True,
            "has_children": False,
            "checked": None,
        }
    ]


@pytest.mark.anyio
async def test_preview_writes_nothing_and_commit_reports_only_new_blocks(api):
    api.get(f"{BASE_URL}/blocks/{PAGE}").respond(200, json=page_block())
    api.get(f"{BASE_URL}/blocks/{HEADING}").respond(200, json=block(HEADING, "heading_4", "Ideas"))
    # Inserting after a block: Notion returns the new block followed by every later sibling.
    append = api.patch(f"{BASE_URL}/blocks/{PAGE}/children").respond(
        200, json={"results": [block(NEW, "to_do", "buy milk"), block(BULLET, "bulleted_list_item", "x")]}
    )
    change = {"operation": "append", "texts": ["buy milk"], "block_type": "to_do", "after_block_id": HEADING}
    async with Client(server.mcp) as c:
        preview = (await c.call_tool("preview_edit", {"page_id": PAGE, "changes": [change]})).structured_content
        assert not append.called
        assert preview["changes"][0]["after"] == "buy milk"
        result = (await c.call_tool("commit_edit", {"preview_id": preview["preview_id"]})).structured_content
    sent = json.loads(append.calls.last.request.content)
    assert sent["position"] == {"type": "after_block", "after_block": {"id": HEADING}}
    assert result["applied"] == 1
    assert result["created_block_ids"] == [NEW]


@pytest.mark.anyio
async def test_preview_id_is_single_use(api):
    api.get(f"{BASE_URL}/blocks/{PAGE}").respond(200, json=page_block())
    api.get(f"{BASE_URL}/blocks/{HEADING}").respond(200, json=block(HEADING, "heading_4", "Ideas"))
    api.delete(f"{BASE_URL}/blocks/{HEADING}").respond(200, json={})
    async with Client(server.mcp) as c:
        unknown = await c.call_tool("commit_edit", {"preview_id": "made-up"}, raise_on_error=False)
        preview = await c.call_tool(
            "preview_edit", {"page_id": PAGE, "changes": [{"operation": "delete", "block_id": HEADING}]}
        )
        pid = preview.structured_content["preview_id"]
        await c.call_tool("commit_edit", {"preview_id": pid})
        reused = await c.call_tool("commit_edit", {"preview_id": pid}, raise_on_error=False)
    assert error_body(unknown)["retryable"] is False
    body = error_body(reused)
    assert body["retryable"] is False
    assert "already committed" in body["error"]


@pytest.mark.anyio
async def test_rate_limit_is_retryable_with_wait(api):
    api.post(f"{BASE_URL}/search").respond(
        429, headers={"Retry-After": "42"}, json={"code": "rate_limited", "message": "slow down"}
    )
    async with Client(server.mcp) as c:
        result = await c.call_tool("find_document", {"query": "x"}, raise_on_error=False)
    body = error_body(result)
    assert body["retryable"] is True
    assert body["retry_after_seconds"] == 42


@pytest.mark.anyio
async def test_401_refreshes_once_and_retries(api):
    search = api.post(f"{BASE_URL}/search")
    search.side_effect = [
        httpx.Response(401, json={"code": "unauthorized"}),
        httpx.Response(200, json={"results": []}),
    ]
    api.post(TOKEN_URL).respond(
        200, json={"access_token": "new-access", "refresh_token": "new-refresh", "bot_id": "bot"}
    )
    async with Client(server.mcp) as c:
        result = await c.call_tool("find_document", {"query": "x"})
    assert result.structured_content == {"result": []}
    saved = server._api.store.load()
    assert (saved.access_token, saved.refresh_token) == ("new-access", "new-refresh")


@pytest.mark.anyio
async def test_dead_refresh_token_asks_user_to_reauthorize(api):
    api.post(f"{BASE_URL}/search").respond(401, json={"code": "unauthorized"})
    api.post(TOKEN_URL).respond(400, json={"error": "invalid_grant"})
    async with Client(server.mcp) as c:
        result = await c.call_tool("find_document", {"query": "x"}, raise_on_error=False)
    body = error_body(result)
    assert body["retryable"] is False
    assert "python -m notion_mcp.auth" in body["hint"]


@pytest.mark.anyio
async def test_schema_rejects_bad_arguments():
    async with Client(server.mcp) as c:
        too_many = await c.call_tool("find_document", {"query": "x", "limit": 100}, raise_on_error=False)
        no_text = await c.call_tool(
            "preview_edit",
            {"page_id": PAGE, "changes": [{"operation": "replace", "block_id": HEADING}]},
            raise_on_error=False,
        )
    assert too_many.is_error
    assert no_text.is_error


@pytest.mark.anyio
async def test_page_delete_is_previewed_but_nothing_lands_in_it_afterwards(api):
    api.get(f"{BASE_URL}/blocks/{PAGE}").respond(200, json=page_block())
    api.get(f"{BASE_URL}/blocks/{HEADING}").respond(200, json=block(HEADING, "heading_4", "Ideas"))
    delete_page = {"operation": "delete", "block_id": PAGE}
    async with Client(server.mcp) as c:
        alone = await c.call_tool("preview_edit", {"page_id": PAGE, "changes": [delete_page]})
        then_append = await c.call_tool(
            "preview_edit",
            {"page_id": PAGE, "changes": [delete_page, {"operation": "append", "texts": ["x"]}]},
            raise_on_error=False,
        )
        then_append_after = await c.call_tool(
            "preview_edit",
            {
                "page_id": PAGE,
                "changes": [delete_page, {"operation": "append", "texts": ["x"], "after_block_id": HEADING}],
            },
            raise_on_error=False,
        )
    assert "deletes the page itself" in alone.structured_content["notes"][0]
    for result in (then_append, then_append_after):
        body = error_body(result)
        assert "which changes[0] deletes" in body["error"]
        assert body["retryable"] is False


@pytest.mark.anyio
async def test_append_after_the_page_itself_is_rejected_at_preview(api):
    api.get(f"{BASE_URL}/blocks/{PAGE}").respond(200, json=page_block())
    change = {"operation": "append", "texts": ["x"], "after_block_id": PAGE}
    async with Client(server.mcp) as c:
        result = await c.call_tool("preview_edit", {"page_id": PAGE, "changes": [change]}, raise_on_error=False)
    assert "Omit after_block_id" in error_body(result)["hint"]


@pytest.mark.anyio
async def test_nested_block_from_another_page_is_rejected(api):
    other_page, other_toggle = "55555555-5555-5555-5555-555555555555", "66666666-6666-6666-6666-666666666666"
    toggle = block(other_toggle, "toggle", "More")
    toggle["parent"] = {"type": "page_id", "page_id": other_page}
    nested = block(BULLET, "bulleted_list_item", "deep")
    nested["parent"] = {"type": "block_id", "block_id": other_toggle}
    api.get(f"{BASE_URL}/blocks/{PAGE}").respond(200, json=page_block())
    api.get(f"{BASE_URL}/blocks/{BULLET}").respond(200, json=nested)
    api.get(f"{BASE_URL}/blocks/{other_toggle}").respond(200, json=toggle)
    async with Client(server.mcp) as c:
        result = await c.call_tool(
            "preview_edit",
            {"page_id": PAGE, "changes": [{"operation": "delete", "block_id": BULLET}]},
            raise_on_error=False,
        )
    assert "different page" in error_body(result)["error"]


@pytest.mark.anyio
async def test_commit_stops_at_first_failure_and_reports_progress(api):
    api.get(f"{BASE_URL}/blocks/{PAGE}").respond(200, json=page_block())
    api.get(f"{BASE_URL}/blocks/{HEADING}").respond(200, json=block(HEADING, "heading_4", "Ideas"))
    api.get(f"{BASE_URL}/blocks/{BULLET}").respond(200, json=block(BULLET, "bulleted_list_item", "x"))
    api.patch(f"{BASE_URL}/blocks/{HEADING}").respond(200, json={})
    api.delete(f"{BASE_URL}/blocks/{BULLET}").respond(404, json={"code": "object_not_found", "message": "gone"})
    append = api.patch(f"{BASE_URL}/blocks/{PAGE}/children").respond(200, json={"results": []})
    changes = [
        {"operation": "replace", "block_id": HEADING, "text": "Plans"},
        {"operation": "delete", "block_id": BULLET},
        {"operation": "append", "texts": ["never sent"]},
    ]
    async with Client(server.mcp) as c:
        preview = await c.call_tool("preview_edit", {"page_id": PAGE, "changes": changes})
        result = await c.call_tool("commit_edit", {"preview_id": preview.structured_content["preview_id"]})
    assert result.structured_content["applied"] == 1
    assert result.structured_content["error"]["retryable"] is False
    assert not append.called


@pytest.mark.anyio
async def test_commit_with_nothing_applied_is_a_tool_error(api):
    api.get(f"{BASE_URL}/blocks/{PAGE}").respond(200, json=page_block())
    api.get(f"{BASE_URL}/blocks/{HEADING}").respond(200, json=block(HEADING, "heading_4", "Ideas"))
    api.delete(f"{BASE_URL}/blocks/{HEADING}").respond(503, json={"message": "unavailable"})
    async with Client(server.mcp) as c:
        preview = await c.call_tool(
            "preview_edit", {"page_id": PAGE, "changes": [{"operation": "delete", "block_id": HEADING}]}
        )
        result = await c.call_tool(
            "commit_edit", {"preview_id": preview.structured_content["preview_id"]}, raise_on_error=False
        )
    body = error_body(result)
    assert body["retryable"] is True
    assert "read_document" in body["hint"]


@pytest.mark.anyio
async def test_expired_preview_is_rejected(api, monkeypatch):
    api.get(f"{BASE_URL}/blocks/{PAGE}").respond(200, json=page_block())
    monkeypatch.setattr(server, "PREVIEW_TTL_SECONDS", -1)
    async with Client(server.mcp) as c:
        preview = await c.call_tool(
            "preview_edit", {"page_id": PAGE, "changes": [{"operation": "append", "texts": ["x"]}]}
        )
        result = await c.call_tool(
            "commit_edit", {"preview_id": preview.structured_content["preview_id"]}, raise_on_error=False
        )
    assert "expired" in error_body(result)["error"]


@pytest.mark.anyio
async def test_find_without_query_lists_recent_pages_and_read_shows_checked(api):
    search = api.post(f"{BASE_URL}/search").respond(200, json={"results": []})
    todo = block(NEW, "to_do", "buy milk")
    todo["to_do"]["checked"] = True
    api.get(f"{BASE_URL}/blocks/{PAGE}").respond(200, json=page_block())
    api.get(f"{BASE_URL}/blocks/{PAGE}/children").respond(200, json={"results": [todo], "has_more": False})
    async with Client(server.mcp) as c:
        await c.call_tool("find_document", {})
        read = (await c.call_tool("read_document", {"page_id": PAGE})).structured_content
    assert json.loads(search.calls.last.request.content)["query"] == ""
    assert read["blocks"][0]["checked"] is True
