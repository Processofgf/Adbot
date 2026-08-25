"""Ban / disable memory.

Two concerns, both backed by Postgres (via db.py) and mirrored in-memory for
O(1) lookups with zero per-send latency:

* group_bans        — (account_id, chat_id) pairs an account must never target again.
* disabled_sessions — sessions frozen/banned/auth-dead and pulled from rotation
                      permanently. Keyed by a session FINGERPRINT (sha256) which
                      we always have, even if the account dies before we can read
                      its Telegram id. account_id is stored as metadata when known,
                      and `notified` guards a fire-once admin log.

Everything degrades gracefully: if the DB pool is unset the sets still work for
the life of the process, matching the rest of the bot.
"""
import asyncio
import hashlib

from config import logger, ADMIN_ID
import db

# (account_id, chat_id) pairs that are permanently skipped.
BANNED_PAIRS: set[tuple[int, int]] = set()
# session fingerprints that are disabled (frozen / banned / auth-dead).
DISABLED_SESSIONS: set[str] = set()
# account_ids known to be disabled (subset with a resolved id) — used to
# self-heal duplicate sessions of the same dead account.
DISABLED_ACCOUNTS: set[int] = set()
# fingerprints we've already logged an admin notice for (fire-once).
NOTIFIED: set[str] = set()


def fingerprint(session: str) -> str:
    return hashlib.sha256(session.encode()).hexdigest()[:32]


async def load_memory():
    """Hydrate the in-memory sets from the DB at startup."""
    pairs, disabled_fps, disabled_ids, notified_fps = await db.load_ban_memory()
    BANNED_PAIRS.update(pairs)
    DISABLED_SESSIONS.update(disabled_fps)
    DISABLED_ACCOUNTS.update(disabled_ids)
    NOTIFIED.update(notified_fps)
    logger.info(
        f"[bans] loaded {len(BANNED_PAIRS)} group ban(s), "
        f"{len(DISABLED_SESSIONS)} disabled session(s)."
    )


def is_group_banned(account_id: int, chat_id: int) -> bool:
    return (account_id, chat_id) in BANNED_PAIRS


def is_session_disabled(session: str) -> bool:
    return fingerprint(session) in DISABLED_SESSIONS


def is_account_disabled(account_id: int | None) -> bool:
    return account_id is not None and account_id in DISABLED_ACCOUNTS


async def record_group_ban(account_id: int, chat_id: int, reason: str):
    """Persist + remember a per-group ban for one account."""
    key = (account_id, chat_id)
    if key in BANNED_PAIRS:
        return
    BANNED_PAIRS.add(key)
    await db.add_group_ban(account_id, chat_id, reason)
    logger.info(f"[bans] group ban remembered acc={account_id} chat={chat_id} ({reason})")


async def disable_account(session: str, account_id: int | None, reason: str) -> bool:
    """Persist + remember a disabled session/account. Returns True if newly disabled."""
    fp = fingerprint(session)
    newly = fp not in DISABLED_SESSIONS
    DISABLED_SESSIONS.add(fp)
    if account_id is not None:
        DISABLED_ACCOUNTS.add(account_id)
    await db.add_disabled_session(fp, account_id, reason)
    if newly:
        logger.warning(f"[bans] session {fp[:8]} (acc={account_id}) DISABLED ({reason})")
    return newly


async def notify_admin_once(session: str, account_id: int | None, reason: str, user_id: int):
    """Fire-once, best-effort admin log. Never raises, never blocks the loop."""
    fp = fingerprint(session)
    if not ADMIN_ID or fp in NOTIFIED:
        return
    NOTIFIED.add(fp)
    await db.mark_notified(fp)

    async def _send():
        try:
            from client import app
            await app.send_message(
                ADMIN_ID,
                f"⚠️ Account auto-removed\n"
                f"• account_id: {account_id if account_id is not None else 'unknown'}\n"
                f"• owner user_id: {user_id}\n"
                f"• reason: {reason}\n"
                f"Pulled from rotation permanently (no manual reactivation).",
            )
            logger.info(f"[bans] admin notified about disabled session {fp[:8]}")
        except Exception as e:
            logger.warning(f"[bans] admin notify failed for {fp[:8]}: {e}")

    asyncio.create_task(_send())
