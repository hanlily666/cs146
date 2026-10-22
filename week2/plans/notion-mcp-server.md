# Notion MCP Server Design Document

## Current Context
- `week2/` contains the course files `assignment.md` and `writeup.md` (a 74-line TODO template), plus this plan. No code yet.
- The repo root is `cs146/weekly_assignment/`, which contains `week1/` and `week2/`. Its remote is the public repo `github.com/hanlily666/cs146`, and nothing has been committed yet. Week 2 work goes under `week2/`.
- Kept out of git: `week1/.claude/` (gitignored), and `research/`, `spec-driven/superpowers/` and the PDF, which live in `cs146/` outside the repo.
- Repo cleanup on 2026-10-21: `week1/xian-hui-scratch/` and `week1/.git` were moved to the macOS Trash.
- A Notion public integration has been created with redirect URI `http://localhost:8765/callback`.
- Assignment: [week2/assignment.md](https://github.com/mihail911/modern-software-dev-assignments/blob/master/week2/assignment.md). Build an MCP server over stdio that wraps a third-party OAuth 2.0 API. Graded mostly on agent ergonomics (30) and OAuth (25).
- API chosen: Notion. The agent should be able to find a page, read it, and edit it (replace a block's text, append blocks, delete a block).
- Tooling follows the course setup: Miniconda env `cs146s` (Python 3.12.15) and Poetry 2.5.1, with `poetry install` run at the repo root. The repo is based on the course repo history (`upstream` = mihail911/modern-software-dev-assignments).
- Deadline: target Sunday 2026-10-25.

## Requirements

### Functional Requirements
- **Tools that compose:** `find_document` → page IDs → `read_document` → block IDs → `preview_edit` → preview_id → `commit_edit`.
- **Edit operations:** replace one block's text, append new blocks, delete a block. One preview can hold several of these.
- **Brake:** preview-then-commit. Nothing changes in Notion until `commit_edit(preview_id)` is called with an ID returned by `preview_edit`.
- **Real OAuth:** authorization-code flow with cached tokens and silent refresh on 401. Never open a browser during a tool call.
- **One start command**, documented in `writeup.md`: `cd week2 && poetry run python -m notion_mcp.server`.
- **Deliverables:** `.mcp.json.example`, at least one test that goes through the MCP protocol, and the course template `week2/writeup.md` with every TODO filled in.

### Non-Functional Requirements
- **Respect Notion rate limits:** 3 requests/s average. On a 429, return a retryable error with `retry_after_seconds` taken from the `Retry-After` header. No auto-retry loop.
- **Small outputs:** only the fields the agent needs. Never pass raw Notion JSON through.
- **Security:** client ID and secret come from env vars. The token file lives outside the repo with mode 600. No secret is ever committed.
- **Stdout is reserved:** in stdio mode stdout carries the MCP protocol, so all logging goes to stderr.

## Design Decisions

### 1. Edit tool shape: one outcome-level `preview_edit`, plus `commit_edit`
Will implement `preview_edit(page_id, changes: list[Change])` and `commit_edit(preview_id)` because:
- **It matches the outcome.** The user's goal is "make these edits to this document", not "call Notion's PATCH block endpoint". One preview covers the whole change set, and the user approves it once.
- **The rules stay in the schema.** `Change` is a discriminated union keyed on `operation`: `replace` requires `block_id` + `text`; `append` requires `texts` and takes an optional `after_block_id`; `delete` requires `block_id`. Pydantic turns this into `oneOf` with a `const` per operation, so a `replace` with no `text` fails validation with a precise message.
- **There are two tools, not one, because annotations are per tool.** `preview_edit` is `readOnlyHint=True`, so a client can run it without asking. `commit_edit` is `destructiveHint=True`. A single `edit_document(mode=preview|commit)` would have to be marked destructive for both modes, and its arguments would have prose rules like "preview_id is required only when mode=commit".
- **`commit_edit` takes only a `preview_id`,** so the agent cannot commit something the user never saw.
- **Alternatives considered:**
  - One preview tool per operation (6 tools). This mirrors Notion's endpoints rather than the user's goal, and a multi-part edit needs several previews.
  - `edit_document(..., dry_run=True)`. The agent can skip straight to `dry_run=False`, so it's a weaker brake.

### 2. Pending previews are kept in memory
Will store previews in a dict on the server, keyed by a uuid4, single-use, expiring after 10 minutes, because:
- This is the simplest option. A restart losing pending previews is acceptable.
- Alternative considered: a stateless signed token containing the whole edit. That's more complex and gives no benefit for a single local user.

### 3. Errors are raised as `ToolError` with a JSON body
Will raise `fastmcp.exceptions.ToolError(json.dumps({"error", "retryable", "retry_after_seconds"?, "hint"}))` because:
- This sets the protocol's `isError: true`, so the agent and the client both know the call failed. The JSON body is what the agent reads to decide whether to retry.
- Alternative considered: returning `{"error": ...}` as a normal result. The agent could mistake it for success, and it pollutes each tool's output schema.
- Status mapping:

| Notion response | `retryable` | `hint` |
|---|---|---|
| 400 / validation error | false | fix the arguments |
| 401, refresh failed or no token file | false | run `poetry run python -m notion_mcp.auth` |
| 403 | false | the page isn't shared with the integration |
| 404 | false | ID not found; get IDs from `find_document` / `read_document` |
| 429 | true | includes `retry_after_seconds` |
| 500 / 502 / 503 / 529 / network error | true | |

### 4. OAuth: a separate auth CLI, and the server refreshes reactively
- `poetry run python -m notion_mcp.auth` does the one-time browser flow:
  - Starts a local callback server on `http://localhost:8765/callback`.
  - Opens `https://api.notion.com/v1/oauth/authorize?owner=user&response_type=code&client_id=…&redirect_uri=…&state=…`.
  - Checks `state`, then POSTs the code to `/v1/oauth/token` (HTTP Basic auth, `grant_type=authorization_code`).
  - Saves `{access_token, refresh_token, workspace_name, bot_id}` to `~/.config/notion-mcp/token.json`.
- The server never opens a browser.
  - On a 401: call `grant_type=refresh_token` once, save the **new pair** atomically (Notion rotates refresh tokens), then retry the original request once.
  - If the refresh fails, raise a non-retryable error that tells the user to run the auth command.
- Refresh happens only on a 401 because Notion's token response has no `expires_in`, so there is no expiry time to refresh ahead of.
- **Scopes:** Notion has no scope parameter in the URL. Permissions are capabilities set on the integration:
  - **Read content:** `find_document`, `read_document`, previews.
  - **Update content:** replace and delete (delete is a soft-delete to trash).
  - **Insert content:** append.
  - **No comments and no user info**, because no tool needs them.

### 5. Stack
Will use Python 3.12 (conda env `cs146s`), FastMCP, httpx, and pydantic, managed by the course's root Poetry project, because:
- It follows the course setup (conda + `poetry install` at the repo root), so TAs and later weeks use the same environment.
- Root `pyproject.toml` uses `package-mode = false`, so there are no console scripts. Every command is `poetry run python -m …`, run from `week2/` so that `notion_mcp` is importable.
- Plain `httpx` is used instead of the `notion-client` SDK. Only 7 endpoints are needed, and controlling the 401 refresh and retry path directly is simpler.
- `@pytest.mark.anyio` is used for async tests. The anyio library comes with fastmcp, so `pytest-asyncio` isn't needed.

## Technical Design

### 1. Core Components
```python
# notion_mcp/auth.py
def main() -> None:
    """`python -m notion_mcp.auth`: browser flow, writes token file. Only place a browser opens."""

# notion_mcp/tokens.py
class TokenStore:
    path: Path  # ~/.config/notion-mcp/token.json, chmod 600
    def load(self) -> Tokens | None: ...
    def save(self, tokens: Tokens) -> None: ...  # write temp file + os.replace (atomic)
    def refresh(self) -> Tokens: ...  # POST /v1/oauth/token grant_type=refresh_token; raises AuthError

# notion_mcp/notion_api.py
class NotionAPI:
    """Thin httpx wrapper. Adds Authorization + Notion-Version: 2026-03-11.
    On 401: TokenStore.refresh() once, retry once. Maps HTTP errors -> NotionError."""
    def search_pages(self, query: str, limit: int) -> list[dict]: ...      # POST /v1/search filter object=page
    def get_page(self, page_id: str) -> dict: ...                          # GET /v1/pages/{id}
    def get_block(self, block_id: str) -> dict: ...                        # GET /v1/blocks/{id}
    def list_children(self, block_id: str, cursor: str | None) -> dict: ...# GET /v1/blocks/{id}/children
    def update_block_text(self, block_id: str, block_type: str, text: str) -> dict: ...  # PATCH /v1/blocks/{id}
    def append_children(self, parent_id: str, blocks: list[dict], after_block_id: str | None) -> dict: ...  # PATCH /v1/blocks/{id}/children, position after_block
    def delete_block(self, block_id: str) -> dict: ...                     # DELETE /v1/blocks/{id}

# notion_mcp/server.py
mcp = FastMCP("notion", instructions="Workflow: find_document -> [read_document, only when targeting specific blocks] -> preview_edit -> show the preview to the user -> commit_edit(preview_id). Appending to the end of a page needs only page_id.")

@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
def find_document(query: Annotated[str, Field(min_length=1)],
                  limit: Annotated[int, Field(ge=1, le=25)] = 10) -> list[PageSummary]: ...

@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
def read_document(page_id: str, cursor: str | None = None) -> PageContent:
    """page_id: from find_document, or a block_id from read_document whose has_children is true."""

@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
def preview_edit(page_id: str,
                 changes: Annotated[list[Change], Field(min_length=1, max_length=20)]) -> EditPreview:
    """page_id: from find_document. block_ids in changes: from read_document of this page.
    Fetches current text of targeted blocks to show before/after. Changes nothing in Notion."""

@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True})
def commit_edit(preview_id: str) -> EditResult:
    """Applies a preview in order. preview_id: from preview_edit, single-use, expires after 10 min."""
```

### 2. Data Models
```python
AppendBlockType = Literal["paragraph", "heading_1", "heading_2", "heading_3",
                          "bulleted_list_item", "numbered_list_item", "to_do", "quote"]

class PageSummary(BaseModel):
    page_id: str; title: str; url: str; last_edited_time: str

class BlockSummary(BaseModel):
    block_id: str; type: str; text: str
    editable: bool       # True if operation=replace supports this type (has rich_text)
    has_children: bool   # pass block_id to read_document to read nested blocks

class PageContent(BaseModel):
    page_id: str; title: str; blocks: list[BlockSummary]; next_cursor: str | None

Text = Annotated[str, Field(min_length=1, max_length=2000)]  # Notion rich_text content limit

class ReplaceChange(BaseModel):
    operation: Literal["replace"]; block_id: str; text: Text

class AppendChange(BaseModel):
    operation: Literal["append"]
    texts: Annotated[list[Text], Field(min_length=1, max_length=100)]  # one block per item
    block_type: AppendBlockType = "paragraph"
    after_block_id: str | None = None  # None = end of page

class DeleteChange(BaseModel):
    operation: Literal["delete"]; block_id: str

Change = Annotated[ReplaceChange | AppendChange | DeleteChange, Field(discriminator="operation")]

class ChangePreview(BaseModel):
    operation: Literal["replace", "append", "delete"]
    block_id: str | None; before: str | None; after: str | None

class EditPreview(BaseModel):
    preview_id: str; page_id: str; changes: list[ChangePreview]; expires_at: str
    note: str  # e.g. "replace drops inline formatting (bold/links) in this block"

class EditResult(BaseModel):
    applied: int; failed: int
    results: list[dict]  # per change: {operation, block_ids, ok, error?}; stops at first failure

class Tokens(BaseModel):
    access_token: str; refresh_token: str | None; workspace_name: str | None; bot_id: str
```

### 3. Integration Points
- Notion REST API `https://api.notion.com/v1`, header `Notion-Version: 2026-03-11`.
- Claude Code launches the server over stdio via `.mcp.json`:

  ```json
  {"command": "<conda>/envs/cs146s/bin/python", "args": ["-m", "notion_mcp.server"],
   "env": {"PYTHONPATH": "<abs>/week2", "NOTION_CLIENT_ID": "…", "NOTION_CLIENT_SECRET": "…"}}
  ```
- Data flow:

  ```
  Agent ─stdio─▶ server.py ─▶ NotionAPI ─https─▶ Notion
                    │              │ 401 → TokenStore.refresh → retry once
                    └ previews{}   └ token.json (~/.config/notion-mcp)
  ```

### 4. Files Changed
Existing files to modify:
- `pyproject.toml` (repo root): add `fastmcp` and `httpx` to `[tool.poetry.dependencies]` (lines 10-18) and `respx` to `[tool.poetry.group.dev.dependencies]` (lines 20-25), via `poetry add`.
- `poetry.lock` (repo root): regenerated by `poetry add`.
- `week2/writeup.md`: replace every TODO in the course template (lines 1-74): Part I lines 5-17, Part II lines 24-33, Part III lines 38-48, Part IV lines 53-67.

New files:
- `week2/notion_mcp/__init__.py` (new, empty)
- `week2/notion_mcp/tokens.py` (new, ~60 lines)
- `week2/notion_mcp/auth.py` (new, ~80 lines)
- `week2/notion_mcp/notion_api.py` (new, ~120 lines)
- `week2/notion_mcp/server.py` (new, ~200 lines): tools, models, preview store, error mapping
- `week2/tests/test_protocol.py` (new, ~150 lines)
- `week2/.mcp.json.example` (new). `.mcp.json` and `.env` are already gitignored at the repo root.

## Implementation Plan

### Phase 0: Setup (Wed–Thu)
- ~~Repo setup~~ (done: based on course repo history, remote `origin` = hanlily666/cs146, course `.gitignore` + `week1/.claude/` and `.mcp.json`; conda env + Poetry installed).
- Make the first commit (`.gitignore`, `week1/writeup.md`, this plan) **before** any code; check `git status` first.
- ~~Create a Notion public integration~~ (done; Notion accepted the http localhost redirect URI). Confirm the capabilities are Read, Update, Insert content, with no comments and no user info.
- Create a scratch Notion page to test on.
- ✅ Check: you have a client ID and secret exported in your shell, and nothing is in the repo.

### Phase 1: OAuth + API client (Thu)
- `poetry add fastmcp httpx` and `poetry add --group dev respx` (from the repo root, with `cs146s` active).
- Write `tokens.py`, `auth.py`, and the 401 → refresh → retry path in `notion_api.py`.
- ✅ Check: `poetry run python -m notion_mcp.auth` creates the token file; a one-off `search_pages("")` returns your scratch page; manually editing the access token in the file then making a call triggers a refresh, and the file now holds a new pair.

### Phase 2: Read tools (Fri)
- Write `find_document`, `read_document`, and the error mapping in `server.py`.
- ✅ Check: in `npx @modelcontextprotocol/inspector poetry run python -m notion_mcp.server` (from `week2/`), find → read chains; a bad page_id returns a non-retryable 404 error.

### Phase 3: Edit tools (Sat)
- Write the `Change` union, `preview_edit`, the preview store, and `commit_edit`.
- ✅ Check: in the Inspector, a preview with replace + append + delete makes no change in Notion; commit applies all three; reusing or expiring a preview_id gives a clear error.

### Phase 4: Tests, integration, writeup (Sat–Sun)
- Write `tests/test_protocol.py` and `.mcp.json.example`; register the server with `claude mcp add` (or a local `.mcp.json`).
- **Watch the agent use the tools**: give Claude Code 3–4 real prompts, note any wrong tool choice or bad arguments, then fix the matching docstring or schema. The template asks for "one thing you changed after watching the agent misuse a tool" (`writeup.md` line 32).
- Record one chained transcript (e.g. "find my scratch page and add a TODO under the heading X"), plus one failure (bad ID, or revoke access in Notion mid-session).
- Fill every TODO in `week2/writeup.md`. The Part II table needs `file:line` pointers into the final code.
- ✅ Check: `cd week2 && poetry run python -m pytest tests` passes; no TODO remains in `writeup.md`; `git status` shows no `.mcp.json` or token file.

## Testing Strategy

### Unit Tests
None as separate files. The protocol tests below cover the logic. Only Notion's HTTP calls are mocked, using `respx`; our own code is never mocked.

### Integration Tests
`tests/test_protocol.py` uses `fastmcp.Client(mcp)` (in-memory MCP transport), so every call goes through the protocol. The `TokenStore` points at a temp file.
1. `list_tools` returns the 4 tools; find, read, and `preview_edit` have `readOnlyHint=True`; `commit_edit` has `destructiveHint=True`.
2. `find_document` → `read_document` chain: output contains only the shaped fields, and the IDs from find are accepted by read.
3. `preview_edit` with replace + append + delete makes no PATCH or DELETE call; `commit_edit(preview_id)` sends the expected three requests, in order.
4. `commit_edit` with an unknown or already-used preview_id → `isError`, `retryable=false`.
5. Notion returns 429 with `Retry-After: 42` → `isError`, `retryable=true`, `retry_after_seconds=42`.
6. 401, then refresh succeeds → the call succeeds, and the token file holds the rotated pair.
7. 401, then refresh returns `invalid_grant` → a non-retryable error whose hint names `python -m notion_mcp.auth`.
8. `find_document(limit=100)` is rejected by schema validation.
9. `preview_edit` with `{"operation": "replace", "block_id": "…"}` (no `text`) is rejected by schema validation.
10. `commit_edit` where the 2nd of 3 changes gets a 404 → `applied=1, failed=1`, the 3rd change is not sent, and the result says which change failed.

## Observability

### Logging
- stderr only (stdout carries the protocol). INFO for each tool call (tool name, status, Notion `request_id`) and for token refreshes; tokens are never logged.

### Metrics

## Future Considerations

### Potential Enhancements
- Preserve inline formatting on replace; detect a stale preview by comparing `last_edited_time` at commit; a `create_document` tool.

### Known Limitations
- Replace writes plain text and drops formatting (the preview says so). Previews are lost on restart. `read_document` returns one nesting level per call.
- Commits are not atomic, because Notion has no transactions. `commit_edit` stops at the first failure and reports exactly which changes were applied.

## Dependencies

### Development Dependencies
- Conda env `cs146s` + Poetry (course setup). Added via `poetry add`: `fastmcp`, `httpx` (runtime); `respx` (dev). Already present: `pytest`, `pydantic`. MCP Inspector via `npx` (no install).

## Security Considerations
- Secrets only in env vars or the gitignored `.mcp.json`. The token file is outside the repo with mode 600, written atomically. The OAuth `state` is checked to prevent CSRF. Capabilities are minimal and justified in the writeup.

## Rollout Strategy

## References
- Assignment: https://github.com/mihail911/modern-software-dev-assignments/blob/master/week2/assignment.md
- Notion authorization: https://developers.notion.com/docs/authorization
- Notion token / refresh: https://developers.notion.com/reference/create-a-token, https://developers.notion.com/reference/refresh-a-token
- Notion request limits: https://developers.notion.com/reference/request-limits
- FastMCP: https://github.com/jlowin/fastmcp
