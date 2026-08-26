---
feature: token-exchange-fixes
status: delivered
updated: 2026-08-26
branch: dota-tokens
commits: 2eef37b..471b1a1
---

# Token Exchange Fixes

## Report

**What was built** — Three fixes to the Dota 2 token exchange system: (1) Persistent view now correctly reuses the existing message on bot restart by searching oldest-first for the message with `token_select`/`token_create` custom_ids and storing its ID for direct refresh; (2) Private notification threads are created immediately at request creation time, with the Close button moved from the public channel into the thread; (3) `/deals` command now requires a member parameter, filters by that user, and supports up to 5 SelectMenus (125 deals) with pagination. UserManageView is wired into the deal action flow.

**Verification** — `py_compile` and `ruff check` pass on `cogs/dota_tokens.py`.

**Journey log**:
- All view custom_ids were made unique per instance (uuid for DealsMenuView, request_id for DealActionView/ThreadCloseView, request_id+token_id for TokenConfirmView) to prevent conflicts when multiple views coexist.
- `TokenConfirmView` was changed from decorated buttons to programmatic `add_item()` to support unique custom_ids.
- `selectinload(TokenRequest.tokens)` was added to the active requests query in `_setup_persistent_view` to support registering TokenConfirmViews for each token.

## [S1] Problem

Three issues in the current token exchange system:

1. **Persistent view duplication on restart**: When the bot restarts, `_setup_persistent_view()` searches the last 10 messages for a bot message with components and edits it. In practice, it often fails to find the existing message and sends a new one, creating duplicates. The user wants the bot to fetch messages, find the earliest bot message with components, verify it's the persistent view, and reuse it.

2. **Private thread created too late**: The private notification thread for the requester is only created when a fulfiller sends a token. The requester needs the thread at deal creation time so they can close the deal from there. The "Close" button visible to everyone in the main channel is confusing — it should only be in the private thread.

3. **Deals command SelectMenu overflow**: `/deals` puts all active deals into a single SelectMenu (max 25 options). With more than 25 deals, Discord returns HTTP 400. The command also doesn't filter by user, making it hard to manage.

## [S2] Design

### D1: Persistent view reuse on restart

**Current behavior**: `_setup_persistent_view()` iterates `channel.history(limit=10)` and edits the first bot message with components. If not found, sends a new message.

**New behavior**:
1. Fetch messages from the channel (limit=50, oldest first via `oldest_first=True`).
2. Find the **earliest** bot message that has components (this is the persistent view).
3. If found: edit it with the current view + embed. Do NOT send a new message.
4. If not found: send a new persistent view message.
5. Store the persistent view message ID in the cog instance (`self._persistent_msg_id`) so `_refresh_select_menu()` can use it directly without searching.

**Edge case**: If the earliest bot message with components is NOT the persistent view (e.g., it's a request embed), skip it and look for the next one. The persistent view is identified by having `custom_id="token_select"` or `custom_id="token_create"` in its components.

### D2: Private thread at request creation + Close button relocation

**Current behavior**: 
- Request embed is sent to the main channel with a SelectMenu (for fulfillers) + Close button (for requester).
- Private thread is created lazily when the first fulfiller sends a token.

**New behavior**:
1. When a request is created (`_do_create_request`), immediately create a private thread for the requester.
2. Send a welcome message in the private thread with a "Закрыть сделку" button.
3. Remove the Close button from the main channel request embed. The main channel embed keeps only the SelectMenu for fulfillers.
4. When the requester clicks "Закрыть сделку" in the private thread:
   - Cancel all unfulfilled tokens.
   - Fulfilled tokens keep their points.
   - Delete the request embed from the main channel.
   - Delete the private thread (or archive it).
5. The private thread also receives fulfillment notifications (as it does now).

**Thread naming**: `Сделка #{request_id}` (same as current).

### D3: Deals command with pagination

**Current behavior**: `/deals` shows all active + closed deals in up to 2 SelectMenus (active + closed), each capped at 25 options.

**New behavior**:
1. `/deals` requires a mandatory `member` parameter: `/deals <@member>`.
2. Filter deals by that member (as requester).
3. Split results into chunks of 25, creating up to 5 SelectMenus total (max 125 deals displayed).
4. Each SelectMenu is labeled with a page indicator: "Сделки 1-25", "Сделки 26-50", etc.
5. Active and closed deals are shown separately (active first, then closed).
6. The embed shows summary stats for the member.

## [S3] Out of Scope

- No database schema changes needed (all fields already exist).
- No new migrations.
- No changes to the auto-confirm loop.
- No changes to the dispute flow.
- No changes to the `/profile` command.

## Tasks

- [x] T1: Fix persistent view reuse — acceptance: bot restart does not create duplicate persistent view messages; existing message is found and edited (covers: D1)
- [x] T2: Create private thread at request creation — acceptance: private thread is created immediately when a request is made, with a welcome message and close button (covers: D2)
- [x] T3: Remove Close button from main channel embed — acceptance: main channel request embed only shows SelectMenu, no Close button visible to others (covers: D2)
- [x] T4: Add Close button to private thread — acceptance: requester can close deal from private thread; closing cancels unfulfilled tokens, preserves fulfilled points, deletes main embed (covers: D2)
- [x] T5: Paginate /deals command — acceptance: /deals requires member param, handles >25 deals with multiple SelectMenus (up to 5), shows page indicators (covers: D3)
- [x] T6: Wire UserManageView into /deals — acceptance: selecting a deal from /deals shows block/unblock buttons for the deal's requester (covers: D3)
- [x] T7: Python syntax check — acceptance: py_compile and ruff check pass on modified files (covers: all)
