"""Neon Postgres persistence via asyncpg.

Stores each user's state as a JSONB blob keyed by user_id. Runs in
graceful degrade mode (no-op) when NEON_DATABASE_URL is unset — so the
bot still boots on a plain host without a DB.
"""
import json
import asyncpg

from config import NEON_DATABASE_URL, logger

_pool: asyncpg.Pool | None = None


async def init_db():
    global _pool
    if not NEON_DATABASE_URL:
        logger.warning("[db] NEON_DATABASE_URL not set — persistence disabled.")
        return
    _pool = await asyncpg.create_pool(NEON_DATABASE_URL, min_size=1, max_size=4)
    async with _pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS vexora_users (
                user_id     BIGINT PRIMARY KEY,
                state       JSONB NOT NULL DEFAULT '{}'::jsonb,
                updated_at  TIMESTAMPTZ DEFAULT now()
            )
        """)
        # Per-group ban memory: an account never targets this chat again.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS group_bans (
                account_id  BIGINT NOT NULL,
                chat_id     BIGINT NOT NULL,
                reason      TEXT,
                banned_at   TIMESTAMPTZ DEFAULT now(),
                PRIMARY KEY (account_id, chat_id)
            )
        """)
        # Frozen / banned / auth-dead accounts pulled from rotation for good.
        # Keyed by session fingerprint (always known, even if the account dies
        # before we can read its Telegram id); account_id stored when known.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS disabled_sessions (
                session_hash  TEXT PRIMARY KEY,
                account_id    BIGINT,
                reason        TEXT,
                disabled_at   TIMESTAMPTZ DEFAULT now(),
                notified      BOOLEAN NOT NULL DEFAULT FALSE
            )
        """)
    logger.info("[db] Connected to Neon Postgres.")


async def load_ban_memory():
    """Return (banned_pairs, disabled_fps, disabled_account_ids, notified_fps)."""
    if not _pool:
        return set(), set(), set(), set()
    try:
        async with _pool.acquire() as conn:
            ban_rows = await conn.fetch("SELECT account_id, chat_id FROM group_bans")
            dis_rows = await conn.fetch(
                "SELECT session_hash, account_id, notified FROM disabled_sessions"
            )
    except Exception as e:
        logger.warning(f"[db] load_ban_memory failed: {e}")
        return set(), set(), set(), set()
    pairs = {(r["account_id"], r["chat_id"]) for r in ban_rows}
    disabled_fps = {r["session_hash"] for r in dis_rows}
    disabled_ids = {r["account_id"] for r in dis_rows if r["account_id"] is not None}
    notified_fps = {r["session_hash"] for r in dis_rows if r["notified"]}
    return pairs, disabled_fps, disabled_ids, notified_fps


async def add_group_ban(account_id: int, chat_id: int, reason: str):
    if not _pool:
        return
    try:
        async with _pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO group_bans (account_id, chat_id, reason)
                VALUES ($1, $2, $3)
                ON CONFLICT (account_id, chat_id) DO NOTHING
                """,
                account_id, chat_id, reason,
            )
    except Exception as e:
        logger.warning(f"[db] add_group_ban({account_id},{chat_id}) failed: {e}")


async def add_disabled_session(session_hash: str, account_id: int | None, reason: str):
    if not _pool:
        return
    try:
        async with _pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO disabled_sessions (session_hash, account_id, reason)
                VALUES ($1, $2, $3)
                ON CONFLICT (session_hash)
                DO UPDATE SET account_id = COALESCE(disabled_sessions.account_id, EXCLUDED.account_id)
                """,
                session_hash, account_id, reason,
            )
    except Exception as e:
        logger.warning(f"[db] add_disabled_session({session_hash[:8]}) failed: {e}")


async def mark_notified(session_hash: str):
    if not _pool:
        return
    try:
        async with _pool.acquire() as conn:
            await conn.execute(
                "UPDATE disabled_sessions SET notified = TRUE WHERE session_hash = $1",
                session_hash,
            )
    except Exception as e:
        logger.warning(f"[db] mark_notified({session_hash[:8]}) failed: {e}")


def _sanitize(state: dict) -> dict:
    """Drop transient / non-serialisable bits before storing."""
    out = {}
    for k, v in state.items():
        if k == "login_data":
            continue  # holds a live Client instance
        out[k] = v
    return out


async def save_state(user_id: int, state: dict):
    if not _pool:
        return
    payload = json.dumps(_sanitize(state))
    try:
        async with _pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO vexora_users (user_id, state, updated_at)
                VALUES ($1, $2::jsonb, now())
                ON CONFLICT (user_id)
                DO UPDATE SET state = $2::jsonb, updated_at = now()
                """,
                user_id, payload,
            )
    except Exception as e:
        logger.warning(f"[db] save_state({user_id}) failed: {e}")


async def load_all() -> dict[int, dict]:
    if not _pool:
        return {}
    try:
        async with _pool.acquire() as conn:
            rows = await conn.fetch("SELECT user_id, state FROM vexora_users")
    except Exception as e:
        logger.warning(f"[db] load_all failed: {e}")
        return {}
    result = {}
    for row in rows:
        raw = row["state"]
        data = json.loads(raw) if isinstance(raw, str) else dict(raw)
        # Restore transient fields
        data["login_data"] = {}
        data.setdefault("waiting_for", None)
        result[row["user_id"]] = data
    logger.info(f"[db] Loaded {len(result)} user state(s) from Neon.")
    return result


async def delete_user(user_id: int):
    if not _pool:
        return
    try:
        async with _pool.acquire() as conn:
            await conn.execute("DELETE FROM vexora_users WHERE user_id = $1", user_id)
    except Exception as e:
        logger.warning(f"[db] delete_user({user_id}) failed: {e}")
