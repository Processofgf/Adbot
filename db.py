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
    logger.info("[db] Connected to Neon Postgres.")


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
