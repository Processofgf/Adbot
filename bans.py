"""Ban / disable memory.

Two concerns, both backed by Postgres (via db.py) and mirrored in-memory for
O(1) lookups with zero per-send latency:

* group_bans      — (account_id, chat_id) pairs an account must never target again.
* disabled_accts  — account_ids (Telegram user id) frozen/banned and pulled from
                    rotation permanently. `notified` guards a fire-once admin log.

account_id is the account's own Telegram user id (client.get_me().id), so the
memory is stable regardless of a session's position in state["sessions"].

Everything degrades gracefully: if the DB pool is unset the sets still work for
the life of the process, matching the rest of the bot.
"""
import asyncio

from config import logger, ADMIN_ID
import db

# (account_id, chat_id) pairs that are permanently skipped.
BANNED_PAIRS: set[tuple[int, int]] = set()
# account_ids that are disabled (frozen / banned / auth-dead).
DISABLED_ACCOUNTS: set[int] = set()
# account_ids we've already logged an admin notice for (fire-once).
NOTIFIED: set[int] = set()


async def load_memory():
    """Hydrate the in-memory sets from the DB at startup."""
    pairs, disabled, notified = await db.load_ban_memory()
    BANNED_PAIRS.update(pairs)
    DISABLED_ACCOUNTS.update(disabled)
    NOTIFIED.update(notified)
    logger.info(
        f"[bans] loaded {len(BANNED_PAIRS)} group ban(s), "
        f"{len(DISABLED_ACCOUNTS)} disabled account(s)."
    )


def is_group_banned(account_id: int, chat_id: int) -> bool:
    return (account_id, chat_id) in BANNED_PAIRS


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


async def disable_account(account_id: int | None, reason: str):
    """Persist + remember a disabled account. Returns True if newly disabled."""
    if account_id is None:
        return False
    newly = account_id not in DISABLED_ACCOUNTS
    DISABLED_ACCOUNTS.add(account_id)
    await db.add_disabled_account(account_id, reason)
    if newly:
        logger.warning(f"[bans] account {account_id} DISABLED ({reason})")
    return newly


async def notify_admin_once(account_id: int, reason: str, user_id: int):
    """Fire-once, best-effort admin log. Never raises, never blocks the loop."""
    if not ADMIN_ID or account_id in NOTIFIED:
        return
    NOTIFIED.add(account_id)
    await db.mark_notified(account_id)

    async def _send():
        try:
            from client import app
            await app.send_message(
                ADMIN_ID,
                f"⚠️ Account auto-removed\n"
                f"• account_id: {account_id}\n"
                f"• owner user_id: {user_id}\n"
                f"• reason: {reason}\n"
                f"Pulled from rotation permanently (no manual reactivation).",
            )
            logger.info(f"[bans] admin notified about disabled account {account_id}")
        except Exception as e:
            logger.warning(f"[bans] admin notify failed for {account_id}: {e}")

    asyncio.create_task(_send())
