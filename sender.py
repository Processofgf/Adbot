"""Broadcast worker and per-account sending logic."""
import asyncio
import random
import time

from pyrogram import Client
from pyrogram.errors import (
    FloodWait, RPCError,
    AuthKeyUnregistered, SessionRevoked,
    AuthKeyDuplicated, UserDeactivated, UserDeactivatedBan,
)

from config import API_ID, API_HASH, logger
from state import USER_STATES, RUNNING_TASKS, VEXORA_CHAT_IDS
from ui import send_user_update
from enforcer import enforce_account

ENFORCE_INTERVAL = 120
_LAST_ENFORCED: dict[tuple[int, int], float] = {}

# ── FloodWait policy ────────────────────────────────────────────────────────
# sleep_threshold=0 on the Client means Pyrogram NEVER auto-sleeps on FloodWait
# — every FloodWait reaches our send_to_group as an exception.
# We then split:
#   ≤ LARGE_FLOOD_THRESHOLD  →  sleep inline + retry  (per-chat slowmode)
#   >  LARGE_FLOOD_THRESHOLD  →  raise _AccountFloodWait  (account rate-limited)
# ────────────────────────────────────────────────────────────────────────────
LARGE_FLOOD_THRESHOLD = 30

_ACCOUNT_COOLDOWN: dict[tuple[int, int], float] = {}
_ROUND_CURSOR:     dict[tuple[int, int], int]   = {}


class _AccountFloodWait(Exception):
    def __init__(self, wait_seconds: int):
        self.wait_seconds = wait_seconds
        super().__init__(f"AccountFloodWait({wait_seconds}s)")


# ── Dead-session auto-removal ───────────────────────────────────────────────
# When Telegram returns AUTH_KEY_UNREGISTERED / SESSION_REVOKED the session is
# permanently unusable.  Keeping it causes:
#   • every round → all accounts fail → consecutive_failures++ → 300 s backoff
#   • "sends a bit then 5-6 minute gap" pattern visible in the logs
# We remove the session immediately and clear per-user cursor/cooldown state
# (index-based, so they must all reset when the list shrinks).
# ────────────────────────────────────────────────────────────────────────────

def _remove_dead_session(user_id: int, session: str, state: dict, reason: str):
    """Remove a dead session by value and reset all index-based state for this user."""
    try:
        idx = state["sessions"].index(session)
        state["sessions"].remove(session)

        # Indices shifted — safest to drop ALL cursor/cooldown entries for this user.
        for mapping in (_ACCOUNT_COOLDOWN, _ROUND_CURSOR):
            stale = [k for k in mapping if k[0] == user_id]
            for k in stale:
                del mapping[k]

        logger.warning(
            f"[acc {idx + 1}] Dead session auto-removed ({reason}). "
            f"{len(state['sessions'])} session(s) remaining."
        )
    except ValueError:
        pass  # Already removed by a concurrent task


# ── Core send primitive ─────────────────────────────────────────────────────

async def send_to_group(
    user_app, chat_id: int, message: str, state: dict, max_net_retries: int = 2
):
    """Send one message to one chat.

    Because the Client is created with sleep_threshold=0, ALL FloodWaits reach
    this function as exceptions — Pyrogram never auto-sleeps for us.
    """
    net_attempt = 0
    while True:
        if state["status"] != "RUNNING":
            return
        try:
            await user_app.send_message(chat_id, message)
            state["sent_count"] += 1
            return
        except FloodWait as e:
            wait = e.value
            logger.info(f"[send] FloodWait {wait}s  chat={chat_id}")
            if wait <= LARGE_FLOOD_THRESHOLD:
                await asyncio.sleep(wait + 1)
            else:
                raise _AccountFloodWait(wait)
        except (ConnectionError, TimeoutError, OSError) as e:
            net_attempt += 1
            if net_attempt > max_net_retries:
                logger.warning(f"[send] network error chat={chat_id}: {e}")
                state["failed_count"] += 1
                return
            await asyncio.sleep(2 ** net_attempt)
        except RPCError as e:
            logger.info(f"[send] RPC error chat={chat_id}: {e}")
            state["failed_count"] += 1
            return
        except Exception as e:
            logger.error(f"[send] unexpected error chat={chat_id}: {e}")
            state["failed_count"] += 1
            return


# ── Per-account round ───────────────────────────────────────────────────────

async def _run_account_round(
    user_id: int, index: int, session: str, state: dict, message: str
) -> bool:
    """Connect one account, send to all its groups with delay, disconnect.

    Returns True  → account started OK (even if all sends were banned).
    Returns False → account could not connect (dead session / network).

    Only False triggers the consecutive-failure backoff in the worker loop.
    Banned-everywhere accounts are alive — they should not cause backoff.

    Dead sessions (AUTH_KEY_UNREGISTERED, SESSION_REVOKED, …) are automatically
    removed from state["sessions"] so they never cause a False return again.
    """
    acc_key = (user_id, index)

    cooldown_until = _ACCOUNT_COOLDOWN.get(acc_key, 0.0)
    if time.monotonic() < cooldown_until:
        remaining = int(cooldown_until - time.monotonic())
        logger.info(f"[acc {index + 1}] cooling down ~{remaining}s — skipped.")
        # Return True: account is alive, just cooling down — don't trigger backoff.
        return True

    started = False
    user_app = Client(
        f"User_{user_id}_Acc_{index}",
        session_string=session,
        api_id=API_ID,
        api_hash=API_HASH,
        in_memory=True,
        no_updates=True,
        sleep_threshold=0,          # Never auto-sleep on FloodWait — our code handles it
        device_model="PC 64bit",
        system_version="Windows 11",
        app_version="4.16.3",
    )

    try:
        await user_app.start()
        started = True

        # Branding / join enforcement
        enforce_key = (user_id, index)
        now = time.monotonic()
        if now - _LAST_ENFORCED.get(enforce_key, 0) >= ENFORCE_INTERVAL:
            try:
                await enforce_account(user_app, do_join=True)
            except Exception as e:
                logger.warning(f"[acc {index + 1}] enforce failed: {e}")
            finally:
                _LAST_ENFORCED[enforce_key] = time.monotonic()

        # Collect eligible groups and apply rotating cursor.
        dialogs = []
        async for dialog in user_app.get_dialogs():
            if dialog.chat and dialog.chat.type.name in ["GROUP", "SUPERGROUP"]:
                if dialog.chat.id not in VEXORA_CHAT_IDS:
                    dialogs.append(dialog)

        if dialogs:
            n = len(dialogs)
            cursor = _ROUND_CURSOR.get(acc_key, 0) % n
            ordered = dialogs[cursor:] + dialogs[:cursor]
            chats_done = 0

            for dialog in ordered:
                while state.get("status") == "PAUSED":
                    await asyncio.sleep(1)
                if state["status"] != "RUNNING":
                    break

                try:
                    await send_to_group(user_app, dialog.chat.id, message, state)
                    chats_done += 1
                    await asyncio.sleep(state["delay"])
                except _AccountFloodWait as fw:
                    logger.warning(
                        f"[acc {index + 1}] account-level FloodWait {fw.wait_seconds}s — "
                        f"round ended, cooldown set."
                    )
                    _ACCOUNT_COOLDOWN[acc_key] = time.monotonic() + fw.wait_seconds + 2
                    _ROUND_CURSOR[acc_key] = (cursor + chats_done + 1) % n
                    break

        return True  # Account was alive — even if all sends were banned

    # ── Dead session errors → auto-remove ──────────────────────────────────
    except (AuthKeyUnregistered, SessionRevoked,
            AuthKeyDuplicated, UserDeactivated, UserDeactivatedBan) as e:
        reason = type(e).__name__
        logger.warning(f"[acc {index + 1}] dead session — {reason}")
        _remove_dead_session(user_id, session, state, reason)
        await send_user_update(
            user_id,
            f"🗑️ **Account {index + 1}:** Session dead (`{reason}`) — auto-removed.\n"
            f"📊 {len(state['sessions'])} account(s) remaining.",
        )
        return False  # This slot is gone; don't count other live accounts as failed

    # ── Network errors ──────────────────────────────────────────────────────
    except (ConnectionError, TimeoutError, OSError) as e:
        logger.error(f"[acc {index + 1}] network error: {e}")
        await send_user_update(
            user_id, f"⚠️ **Account {index + 1}:** Connection error. Please try again."
        )
        return False

    # ── Any other unexpected error ──────────────────────────────────────────
    except Exception as e:
        logger.error(f"[acc {index + 1}] error: {e}")
        await send_user_update(
            user_id, f"⚠️ **Account {index + 1}:** Unexpected error — {e}"
        )
        return False

    finally:
        if started:
            try:
                await user_app.stop()
            except Exception as e:
                logger.debug(f"[acc {index + 1}] stop() error: {e}")


# ── Broadcast orchestration ─────────────────────────────────────────────────

async def broadcast_once(
    user_id: int, state: dict, message_override: str | None = None
) -> bool:
    """Run one send-round across all accounts.

    NORMAL:   accounts run sequentially.
    ADVANCED: all accounts run simultaneously via asyncio.gather.
              Each account still sends groups sequentially with state["delay"]
              — parallelism is at the account level only, keeping per-account
              rate inside Telegram's comfortable window.

    Returns True if at least one account is alive (started or in cooldown).
    The caller uses this to decide whether to apply consecutive-failure backoff.
    """
    message = message_override or state["message"]
    current_mode = state.get("sending_mode", "NORMAL")
    sessions = list(state["sessions"])

    if not sessions:
        return False

    while state.get("status") == "PAUSED":
        await asyncio.sleep(1)
    if state["status"] != "RUNNING":
        return False

    if current_mode == "ADVANCED":
        results = await asyncio.gather(
            *[
                _run_account_round(user_id, index, session, state, message)
                for index, session in enumerate(sessions)
            ],
            return_exceptions=True,
        )
        return any(r is True for r in results)

    else:
        had_success = False
        for index, session in enumerate(sessions):
            while state.get("status") == "PAUSED":
                await asyncio.sleep(1)
            if state["status"] != "RUNNING":
                break
            if await _run_account_round(user_id, index, session, state, message):
                had_success = True
        return had_success


# ── Worker loops ────────────────────────────────────────────────────────────

async def sleep_with_backoff(attempt: int, base: float = 5.0, cap: float = 60.0):
    """Exponential backoff — capped at 60 s (not 300 s) so recovery is faster."""
    delay = min(base * (2 ** attempt), cap)
    await asyncio.sleep(delay + random.uniform(0, delay * 0.15))


async def dedicated_user_worker(user_id: int):
    """Continuous broadcast loop for manual RUN until STOPPED."""
    consecutive_failures = 0
    try:
        while True:
            state = USER_STATES.get(user_id)
            if not state:
                break
            if state["status"] == "PAUSED":
                await asyncio.sleep(1)
                continue
            if state["status"] != "RUNNING":
                break
            if not state["sessions"]:
                await send_user_update(
                    user_id,
                    "⚠️ **Engine Stopped:** No active accounts. Please add an account.",
                )
                state["status"] = "STOPPED"
                break

            had_success = await broadcast_once(user_id, state)
            if state["status"] != "RUNNING":
                break

            if not had_success:
                consecutive_failures += 1
                logger.warning(
                    f"[Worker] All accounts failed (attempt {consecutive_failures})."
                )
                await sleep_with_backoff(consecutive_failures)
            else:
                consecutive_failures = 0
                await asyncio.sleep(2)

    except asyncio.CancelledError:
        logger.info(f"[Worker] Cancelled  user={user_id}")
    except Exception as e:
        logger.error(f"[Worker] Unhandled error  user={user_id}: {e}", exc_info=True)
    finally:
        RUNNING_TASKS.pop(user_id, None)
        logger.info(f"[Worker] Ended  user={user_id}")


async def run_scheduled_broadcast(
    user_id: int, message_override: str | None = None
):
    """One-shot broadcast triggered by the scheduler."""
    state = USER_STATES.get(user_id)
    if not state:
        return
    if state["status"] == "RUNNING":
        logger.info(f"[Scheduler] user={user_id} already RUNNING — skipped.")
        return
    if not state["sessions"]:
        await send_user_update(user_id, "⚠️ **Scheduled broadcast skipped:** No active accounts.")
        return

    state["status"] = "RUNNING"
    await send_user_update(user_id, "🗓 **Scheduled broadcast starting...**")
    try:
        await broadcast_once(user_id, state, message_override=message_override)
    finally:
        if state["status"] == "RUNNING":
            state["status"] = "STOPPED"
    await send_user_update(user_id, "✅ **Scheduled broadcast finished.**")
