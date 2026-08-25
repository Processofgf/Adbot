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
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS disabled_accounts (
                account_id   BIGINT PRIMARY KEY,
                reason       TEXT,
                disabled_at  TIMESTAMPTZ DEFAULT now(),
                notified     BOOLEAN NOT NULL DEFAULT FALSE
            )
        """)
    logger.info("[db] Connected to Neon Postgres.")


async def load_ban_memory() -> tuple[set[tuple[int, int]], set[int], set[int]]:
    """Return (banned_pairs, disabled_account_ids, notified_account_ids)."""
    if not _pool:
        return set(), set(), set()
    try:
        async with _pool.acquire() as conn:
            ban_rows = await conn.fetch("SELECT account_id, chat_id FROM group_bans")
            dis_rows = await conn.fetch("SELECT account_id, notified FROM disabled_accounts")
    except Exception as e:
        logger.warning(f"[db] load_ban_memory failed: {e}")
        return set(), set(), set()
    pairs = {(r["account_id"], r["chat_id"]) for r in ban_rows}
    disabled = {r["account_id"] for r in dis_rows}
    notified = {r["account_id"] for r in dis_rows if r["notified"]}
    return pairs, disabled, notified


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


async def add_disabled_account(account_id: int, reason: str):
    if not _pool:
        return
    try:
        async with _pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO disabled_accounts (account_id, reason)
                VALUES ($1, $2)
                ON CONFLICT (account_id) DO NOTHING
                """,
                account_id, reason,
            )
    except Exception as e:
        logger.warning(f"[db] add_disabled_account({account_id}) failed: {e}")


async def mark_notified(account_id: int):
    if not _pool:
        return
    try:
        async with _pool.acquire() as conn:
            await conn.execute(
                "UPDATE disabled_accounts SET notified = TRUE WHERE account_id = $1",
                account_id,
            )
    except Exception as e:
        logger.warning(f"[db] mark_notified({account_id}) failed: {e}")


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
