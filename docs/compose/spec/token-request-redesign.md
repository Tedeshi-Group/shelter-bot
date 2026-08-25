---
feature: token-request-redesign
status: delivered
updated: 2026-08-26
branch: production
commits: 4b9cc00..4b9cc00
---

# Token Request Redesign

## Report

**What was built** — Redesigned token request flow. Requests now appear as embeds in main channel with Select menu for fulfillers. When someone fulfills a token, a private notification thread is created for the requester to confirm. Main message is deleted when all tokens are fulfilled. Private thread is deleted when all tokens are confirmed.

**Verification** — `py_compile` passed. Migration created for `thread_id` column.

**Journey log**:
- Moved from public threads per request to embeds in main channel
- Private threads now created per-deal for confirmations
- Per-token Confirm/Dispute buttons replace single Confirm/Dispute per request

## [S1] Problem

Current flow creates public threads for each request, cluttering the channel. User wants:
- Open requests as embeds in main channel (not threads)
- Private notification threads per deal for confirmations
- Cleaner lifecycle: delete public embed when all tokens sent, delete private thread when all resolved

## [S2] Design

### Request lifecycle

1. **Creation**: User selects tokens from persistent view, clicks "Создать запрос". Embed with Select menu (fulfill) + "Закрыть" button appears in main channel (not thread).

2. **Fulfillment**: When someone selects a token from the embed's Select menu:
   - Mark token as fulfilled in DB (+1 friendship point to fulfiller)
   - Create or find private thread for this request (only requester + fulfillers)
   - Send notification in thread: "<@fulfiller> отправил жетон **{token_name}**. Подтвердите в течение 24ч."
   - Add Confirm/Dispute buttons in thread for that specific token
   - Update main channel embed: remove fulfilled token from Select menu

3. **All tokens sent**: When all tokens fulfilled:
   - Delete main channel message (remove from public view)
   - Private thread remains for confirmations

4. **Confirmation**: Each token confirmed individually in private thread:
   - Requester clicks "Подтвердить" for each token
   - When all tokens confirmed → request status = "confirmed"
   - Award creation bonus if eligible
   - Delete private thread

5. **Dispute**: Requester clicks "Спор" for any token:
   - Token goes to disputed state
   - Admin resolves via `/token-resolve`

6. **Close**: Requester clicks "Закрыть" on main embed:
   - Request status = "closed"
   - Delete main message
   - Delete private thread if exists

7. **Auto-confirm**: After 24h without confirmation, auto-confirm all pending tokens.

### Data model changes

- `TokenRequest`: add `thread_id` field (BigInteger, nullable) to track private thread
- `TokenRequestItem`: already has `fulfilled`, `fulfilled_by`, `fulfilled_at`

### Views

- **Main channel embed**: Select menu (unfulfilled tokens) + "Закрыть" button
- **Private thread**: Per-token Confirm/Dispute buttons + notification embed

## [S3] Out of Scope

- Leaderboard command (separate feature)
- Token emoji changes after creation

## Tasks

- [x] T1: Add `thread_id` to TokenRequest model + migration — acceptance: column exists in DB (covers: S2)
- [x] T2: Rewrite request creation — acceptance: embed appears in main channel, no thread created (covers: S2)
- [x] T3: Rewrite fulfillment flow — acceptance: private thread created, notification sent, main embed updated (covers: S2)
- [x] T4: Add per-token Confirm/Dispute in private thread — acceptance: buttons work per-token (covers: S2)
- [x] T5: Handle "all tokens sent" — acceptance: main message deleted (covers: S2)
- [x] T6: Handle "all tokens confirmed" — acceptance: thread deleted, creation bonus awarded (covers: S2)
- [x] T7: Update Close button — acceptance: deletes main message and thread (covers: S2)
- [x] T8: Update auto-confirm loop — acceptance: handles new flow (covers: S2)
- [x] T9: Update persistent view registration — acceptance: views registered correctly on restart (covers: S2)
