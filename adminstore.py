"""Reads user session strings from the shared Neon Postgres DB.

The main bot persists each user's state as a JSONB blob in ``vexora_users``.
The admin bot only needs read access to pull every stored session string.
"""
import json
import asyncpg

from adminconfig import NEON_DATABASE_URL, logger

_pool: asyncpg.Pool | None = None


async def init_pool():
    global _pool
    if not NEON_DATABASE_URL:
        logger.warning("[adminstore] NEON_DATABASE_URL not set — cannot read sessions.")
        return
    _pool = await asyncpg.create_pool(NEON_DATABASE_URL, min_size=1, max_size=4)
    logger.info("[adminstore] Connected to Neon Postgres (read).")


def has_db() -> bool:
    return _pool is not None


async def fetch_sessions() -> list[dict]:
    """Return one entry per stored account across all bot users.

    Each entry: {owner_id, acc_index, session}.
    """
    if not _pool:
        return []
    try:
        async with _pool.acquire() as conn:
            rows = await conn.fetch("SELECT user_id, state FROM vexora_users")
    except Exception as e:
        logger.warning(f"[adminstore] fetch_sessions failed: {e}")
        return []

    out: list[dict] = []
    for row in rows:
        raw = row["state"]
        try:
            data = json.loads(raw) if isinstance(raw, str) else dict(raw)
        except Exception:
            continue
        for idx, session in enumerate(data.get("sessions", []) or []):
            if session:
                out.append({
                    "owner_id":  row["user_id"],
                    "acc_index": idx,
                    "session":   session,
                })
    return out


async def count_users() -> int:
    if not _pool:
        return 0
    try:
        async with _pool.acquire() as conn:
            return await conn.fetchval("SELECT COUNT(*) FROM vexora_users") or 0
    except Exception as e:
        logger.warning(f"[adminstore] count_users failed: {e}")
        return 0
