# Ban/Freeze Hardening + Speed Fix — what changed

Adapted to this bot's real stack: **Pyrogram + Neon Postgres** (the "MongoDB" in
the spec maps to the already-wired Postgres). Accounts are the session strings in
each user's state; an account's stable id is its Telegram user id (`get_me().id`).

## New / changed files
- **error_classifier.py** (new) — pure, no-I/O `classify(err) -> Decision`.
  DISABLE is checked *before* the FloodWait branch, so `FROZEN_METHOD_INVALID`
  (420) is a dead account, never "wait and retry".
- **bans.py** (new) — in-memory `BANNED_PAIRS` / `DISABLED_ACCOUNTS` / `NOTIFIED`
  sets with write-through to Postgres; fire-once, non-blocking admin notify.
- **db.py** — added `group_bans` (PK `(account_id, chat_id)`) and
  `disabled_accounts` tables + loaders/writers. Graceful-degrade when no DB.
- **sender.py** — rewritten:
  - Every account runs as its own concurrent async worker (no serial loop).
  - `AdaptiveLimiter` per account: starts at 5 concurrent sends, halves + pauses
    on FloodWait (that account only), grows back after a clean streak.
  - `sleep(15)` removed; cycles chain back-to-back, idling only when a whole
    cycle sent nothing (all groups banned) to avoid busy-spinning `get_dialogs`.
  - Every send routes failures through `classify()` → one Decision. On
    `SKIP_GROUP` the pair is remembered and the chat drops from all future
    cycles (no `failed_count` inflation). On `DISABLE_ACCT` the account is
    pulled from `state["sessions"]`, the client stopped, admin notified once.
- **config.py** — added optional `ADMIN_ID` env var (Telegram user id for notices).
- **bot.py** — loads ban memory at startup.

## New env var
- `ADMIN_ID` — Telegram user id that receives auto-removal notices (optional;
  notices are skipped if unset).

No new dependencies (asyncpg / pyrogram already present).
