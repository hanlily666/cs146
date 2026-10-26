# Week 2 Write-up

## Part I: The Server

**API chosen**, and why:
> Notion. I keep my course notes there, so I can tell whether a tool does what I'd actually want. Notion also only offers OAuth for public integrations (authorization code + rotating refresh tokens), so the OAuth part is real. Its block model gives a natural chain: search → page ID → blocks → block IDs → edit.

**How to run it** (one command):
```
cd week2 && poetry run python -m notion_mcp.server
```
(One-time setup: export `NOTION_CLIENT_ID` / `NOTION_CLIENT_SECRET`, then `cd week2 && poetry run python -m notion_mcp.auth` to authorize in the browser.)

| Tool | What it does | Read/Write | Composes with |
|---|---|---|---|
| `find_document(query?, limit)` | Title search over pages shared with the integration, newest first; with no `query` it lists the most recently edited pages. Returns `page_id`, title, url, last edited | Read | `page_id` → `read_document`, `preview_edit` |
| `read_document(page_id, cursor)` | Top-level blocks of a page (or a nested block) with `block_id`, type, text, `editable`, `has_children`, and `checked` for to-dos | Read | `block_id` → `preview_edit`; `has_children` block → `read_document` again |
| `preview_edit(page_id, changes)` | Validates a list of replace / append / delete changes and returns before/after text plus a `preview_id`. Changes nothing in Notion | Read (dry run) | `preview_id` → `commit_edit` |
| `commit_edit(preview_id)` | Applies a previewed change set in order; single-use | **Write** | `created_block_ids` → later `preview_edit` |


## Part II: Agent Ergonomics

For each, point at the code (`file:line`) and say what it buys.

| Decision | Where | Why |
|---|---|---|
| Schema-level constraint | `notion_mcp/server.py:125` (`Change` discriminated union), `server.py:76` (`AppendBlockType` Literal), `server.py:325` (`limit` 1–25) | Each operation's required fields are enforced by the JSON schema (`oneOf` + `const operation`), so a `replace` without `text` or `limit=100` fails validation with a precise message before any code runs. No prose rules like "text is required when operation=replace". |
| Output shaping (fields kept vs. dropped) | `server.py:49` (`PageSummary`), `server.py:56` (`BlockSummary`), `server.py:224` (`_block_summary`) | A Notion block is ~40 lines of JSON (annotations, colors, created_by, ...). The agent gets a few fields per block: id, type, plain text, `editable` (can `replace` touch it?), `has_children` (should I read deeper?), and `checked` for to-dos (is it done?). Each one answers a question the agent would otherwise have to guess. |
| Structured errors (retry vs. don't-retry) | `notion_api.py:66` (429/409/5xx → retryable), `server.py:238` (`_hint`), `server.py:261` (`_error_body`), `server.py:148` (`ErrorBody`), `server.py:40` (`COMMIT_RETRY_HINT`) | Every failure is `{error, retryable, retry_after_seconds?, hint}`. A 429 says "retryable, wait 42s"; a 404 says "not retryable, get IDs from find_document"; a dead refresh token says "not retryable, ask the user to run the auth command". A commit that applies nothing is a tool error, not a normal-looking result. A retryable commit failure says to `read_document` first, because a timed-out write may have landed. |
| Docstring that chains tools together | `server.py:32` (`INSTRUCTIONS`), `server.py:329-331`, `server.py:346-347`, `server.py:569-571` | Each ID parameter says where it comes from ("A page_id from find_document", "A block_id from read_document"), and the server instructions spell out the whole workflow including "show the preview to the user". |
| Brake on the write tool | `server.py:554` / `server.py:593` (annotations), `server.py:404` (`_take_preview`), `server.py:439-478` (preview checks), `.claude/settings.json` | Writing is split into `preview_edit` (`readOnlyHint`) and `commit_edit` (`destructiveHint`). `commit_edit` takes only a `preview_id`, so it can only apply something that was previewed, once, within 10 minutes. The preview refuses edits to blocks on another page, even nested ones, and edits that would land inside a block an earlier change deletes, such as appending after deleting the page. After the failure below, the Claude Code project config also has an `ask` rule so a human must approve every commit. |

**One thing you changed after watching the agent misuse a tool:**
> **Misuse:** I asked Claude Code to "add a todo 'buy milk' under the first heading of this page". It called `preview_edit` and then immediately `commit_edit`, without showing me the preview. My server's instructions and the `preview_edit` docstring both say to show the preview and wait for the user's approval. The agent decided my request already counted as approval. The edit happened to be correct, but I never got the chance to stop it.
>
> **Why it happened:** the "wait for approval" rule lived only in prose (server instructions and docstrings). Nothing *enforced* it: `commit_edit` accepts any valid `preview_id` immediately, and in Claude Code's auto mode the client didn't prompt either, even though `commit_edit` is annotated `destructiveHint`. A rule the model is merely asked to follow is a suggestion; it reasons around it when the user's request looks clear enough.
>
> **Fix:** move the approval from the prompt into the client. `week2/.claude/settings.json` now has `{"permissions": {"ask": ["mcp__notion__commit_edit"]}}`, so Claude Code stops and asks me before every commit, in any permission mode. I tested it by previewing a delete of the "buy milk" to-do; the commit triggered an Allow/Deny prompt and only ran after I approved. I picked this over server-side tricks (confirmation codes, delays) because anything that passes through the model can't prove a human approved. The remaining gap is that the prompt shows only the `preview_id`, not the diff; MCP elicitation inside `commit_edit` (or a PreToolUse hook that prints the saved preview) would close that, and would also work in clients other than Claude Code.
>
> **Second bug found in the same session:** that commit returned 22 `created_block_ids` for a single new block. When you insert *after* a block, Notion's response lists the new blocks followed by every later sibling, and `_apply` returned the whole list. Appending at the end of a page hides this, since nothing comes after. Fixed in `notion_api.py:181-182` (`append_text_blocks` keeps the first `len(texts)` IDs), with a regression test that mocks the real response shape (`test_preview_writes_nothing_and_commit_reports_only_new_blocks`).


## Part III: OAuth

**Flow**: how a token is obtained, cached, and refreshed:
> **Obtain:** `python -m notion_mcp.auth` (`notion_mcp/auth.py`) is the only code that opens a browser. It starts a one-shot callback server on `http://localhost:8765/callback`, sends the user to Notion's consent page with a random `state`, checks `state` with `compare_digest` (CSRF), and exchanges the code at `/v1/oauth/token` using HTTP Basic client credentials.
> **Cache:** `TokenStore` (`notion_mcp/tokens.py`) writes the access + refresh token to `~/.config/notion-mcp/token.json` (outside the repo), mode 600, atomically (temp file + `fsync` + `os.replace`). Atomic writes matter because Notion rotates the refresh token on every refresh, so a torn write would lose the only valid one.
> **Refresh:** every request goes through `NotionAPI._request` (`notion_api.py:123`). On a 401 it refreshes once and retries once (`notion_api.py:129-134`). Refresh runs under a lock, and it re-reads the file first, so concurrent calls don't spend the rotated refresh token twice (`notion_api.py:109-121`).

**Scopes requested**, and why each is necessary:
> Notion has no `scope` URL parameter; permissions are *capabilities* set on the integration. I enabled only:
> - **Read content**: `find_document`, `read_document`, and the GETs `preview_edit` uses to build before/after text.
> - **Update content**: `replace` rewrites block text; `delete` moves a block to Notion's trash.
> - **Insert content**: `append` adds new blocks.
>
> **Not requested:** comment read/insert and user information (no email, no user list). No tool needs them. Access is also limited to the pages the user picks on the consent screen.

**Secrets**: what's in env, what's gitignored:
> - **In env:** `NOTION_CLIENT_ID`, `NOTION_CLIENT_SECRET`, and optionally `NOTION_REDIRECT_URI` / `NOTION_MCP_TOKEN_FILE`. They are read via `os.environ` only (`tokens.py:38`). `.mcp.json.example` references them as `${NOTION_CLIENT_ID}` placeholders.
> - **Gitignored at the repo root:** `.mcp.json` and `.env`.
> - **Token file:** lives in `~/.config/notion-mcp/`, outside the repo entirely.
> - **Logging:** tokens are never logged.

**Token dies mid-session**: what the agent sees:
> The server never opens a browser during a tool call. On a 401 it silently refreshes and retries, and the agent sees a normal result. If Notion rejects the refresh token (revoked access, `invalid_grant`), the tool returns:
> `{"error": "token refresh rejected by Notion (invalid_grant)", "retryable": false, "hint": "Ask the user to run `cd week2 && poetry run python -m notion_mcp.auth` to re-authorize Notion, then retry."}`
> The agent stops retrying and tells the user the exact command. Transient refresh failures (network, 429, 5xx) come back as `retryable: true` instead (`notion_api.py:118-121`). A malformed token response is also an `AuthError` (`tokens.py:48-49`), so it never surfaces as a raw traceback. This path is covered by `test_dead_refresh_token_asks_user_to_reauthorize`.


## Part IV: Integration

**Registration config** (`.mcp.json.example`) and the client you used:
> Client: **Claude Code** (CLI), launching the server over stdio. Copy `week2/.mcp.json.example` to `.mcp.json` (gitignored), fill in the two absolute paths, and export the client ID/secret:
> ```json
> {"mcpServers": {"notion": {
>   "command": "/ABSOLUTE/PATH/TO/miniconda3/envs/cs146s/bin/python",
>   "args": ["-m", "notion_mcp.server"],
>   "env": {"PYTHONPATH": "/ABSOLUTE/PATH/TO/weekly_assignment/week2",
>           "NOTION_CLIENT_ID": "${NOTION_CLIENT_ID}",
>           "NOTION_CLIENT_SECRET": "${NOTION_CLIENT_SECRET}"}}}}
> ```
> `week2/.claude/settings.json` adds the `ask` rule on `mcp__notion__commit_edit` (see Part II).

**End-to-end transcript**: the prompt, the tools that fired with their arguments, the result:
```
User: what's on my current page in notion
  find_document {"query": " ", "limit": 5}
    -> [{"page_id": "3fae87a2-6cac-8089-ad2d-c5ba327e7406", "title": "CS 146s", "last_edited_time": "2026-10-23T23:02:00.000Z", ...},
        {"page_id": "3fae87a2-6cac-804b-bef4-f9059671eecb", "title": "Week1", ...}]
  read_document {"page_id": "3fae87a2-6cac-8089-ad2d-c5ba327e7406"}
    -> 27 blocks, incl. {"block_id": "3fee87a2-6cac-80f5-9152-f2d817c40c8d", "type": "heading_4",
                         "text": "Announcing the OSS partners program", "editable": true, ...}
Agent: Your most recently edited page is "CS 146s": notes, a Week1 subpage, an "OSS partners program" list, an "MCP" section...

User: add a todo 'buy milk" under the frist heading of this page
  preview_edit {"page_id": "3fae87a2-...7406",
                "changes": [{"operation": "append", "after_block_id": "3fee87a2-6cac-80f5-9152-f2d817c40c8d",
                             "block_type": "to_do", "texts": ["buy milk"]}]}
    -> {"preview_id": "f403faf6-cfa0-462c-9eb6-349b0df74dc7", "changes": [{"operation": "append", "after": "buy milk", ...}], "notes": []}
  commit_edit {"preview_id": "f403faf6-cfa0-462c-9eb6-349b0df74dc7"}
    -> {"applied": 1, "total": 1, "created_block_ids": [ ...22 ids... ], "error": null}   <- bug, see Part II
  read_document {"page_id": "3fae87a2-...7406"}
    -> heading_4 "Announcing the OSS partners program", then to_do "buy milk", then "Anyscale (Ray)"...
Agent: Added "buy milk" right under the first heading; verified by reading the page back.
```
The page ID from `find_document` feeds `read_document`, the heading's block ID feeds `preview_edit`, and the `preview_id` feeds `commit_edit`: all four tools chained. (This run is also where the agent skipped showing the preview; see Part II.)

**A failure, handled**: what you provoked, what the agent saw, what it did next:
```
Provoked: read a page ID that doesn't exist / isn't shared.
  read_document {"page_id": "00000000-0000-0000-0000-000000000000"}
    -> isError: {"error": "Could not find block with ID: 00000000-0000-0000-0000-000000000000. Make sure the
                 relevant pages and databases are shared with your integration \"MCP server for Notion\".",
                 "retryable": false,
                 "hint": "ID not found, or the page isn't shared with the integration. Get page IDs from
                          find_document and block IDs from read_document."}
Next: retryable=false, so the agent doesn't retry the same ID; it follows the hint and gets a real ID:
  find_document {"query": "CS 146s", "limit": 3}
    -> [{"page_id": "3fae87a2-6cac-8089-ad2d-c5ba327e7406", "title": "CS 146s", ...}]
```
Rate limits take the other branch: a 429 comes back as `{"retryable": true, "retry_after_seconds": 42}` (tested in `test_rate_limit_is_retryable_with_wait`).

**Protocol-level test**: what it covers and how to run it:
> `week2/tests/test_protocol.py` drives the server through `fastmcp.Client(server.mcp)` (in-memory MCP transport), so every call goes through `list_tools` / `call_tool` and schema validation like a real client would. Only Notion's HTTP API is mocked (`respx`), and the token file is a temp file. 15 tests:
> - tool annotations: read-only vs. destructive
> - find → read chaining, with only the shaped fields returned
> - `find_document` with no query lists recent pages; `read_document` reports `checked` on to-dos
> - preview sends no writes; commit sends the positioned append and reports only the new block ID (regression test for the bug above)
> - unknown, reused, or expired `preview_id` → non-retryable error
> - commit stops at the first failure: `applied=1`, the third change is never sent
> - commit where nothing applied → tool error, with a hint to `read_document` first
> - deleting the page previews fine, but any change after it that lands in the page is rejected
> - `after_block_id` set to the page itself is rejected at preview; so is a nested block from another page
> - 429 → `retryable` + `retry_after_seconds=42`
> - 401 → refresh → retry, with the rotated token pair saved
> - dead refresh token → hint with the auth command
> - schema rejects `limit=100` and `replace` without `text`
>
> Run: `cd week2 && poetry run python -m pytest tests` (15 passed).


## Submission
1. `Command (⌘) + F` for `TODO`. No results means you're done.
2. Confirm no tokens, client secrets, cached token file, or real `.mcp.json` are committed.
3. Push all changes to your remote repository and submit via Gradescope.
4. Clean up (optional): remove the server from your agent config, delete your cached token, and revoke the OAuth app's access.
