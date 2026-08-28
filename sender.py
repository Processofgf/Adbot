"""Broadcast worker and per-account sending logic."""
import asyncio
import random
import time

from pyrogram import Client
from pyrogram.errors import FloodWait, RPCError

from config import API_ID, API_HASH, logger
from state import USER_STATES, RUNNING_TASKS, VEXORA_CHAT_IDS
from ui import send_user_update
from enforcer import enforce_account

# How often to re-run branding/join enforcement per account (seconds).
ENFORCE_INTERVAL = 120

_LAST_ENFORCED: dict[tuple[int, int], float] = {}

# ── FloodWait policy ────────────────────────────────────────────────────────
# ≤ LARGE_FLOOD_THRESHOLD  →  per-chat slowmode: sleep inline and retry once.
# >  LARGE_FLOOD_THRESHOLD  →  account-level rate-limit: raise _AccountFloodWait.
#    The account round ends immediately; no inline sleep; other accounts keep going.
# ────────────────────────────────────────────────────────────────────────────
LARGE_FLOOD_THRESHOLD = 30  # seconds

# Per (user_id, session_index) → monotonic timestamp when account may resume.
_ACCOUNT_COOLDOWN: dict[tuple[int, int], float] = {}

# Per (user_id, session_index) → next-round starting index in the dialog list.
# Prevents starvation when the first group keeps flooding.
_ROUND_CURSOR: dict[tuple[int, int], int] = {}


class _AccountFloodWait(Exception):
    """Raised by send_to_group on a large (account-level) FloodWait.
    Caught by _run_account_round to end that account's round without sleeping."""
    def __init__(self, wait_seconds: int):
        self.wait_seconds = wait_seconds
        super().__init__(f"AccountFloodWait({wait_seconds}s)")


# ── Core send primitive ─────────────────────────────────────────────────────

async def send_to_group(
    user_app, chat_id: int, message: str, state: dict, max_net_retries: int = 2
):
    """Send one message to one chat.

    Small FloodWait  → sleep + retry (slowmode honoured).
    Large FloodWait  → raise _AccountFloodWait (caller ends the round).
    Network errors   → limited retries with exponential backoff.
    Any other error  → log + increment failed_count + return.
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
                await asyncio.sleep(wait + 1)   # per-chat slowmode — absorb inline
            else:
                raise _AccountFloodWait(wait)   # account rate-limited — bail out
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
    """Run one full send-round for a single account.

    Connects the account, sends to every eligible group with state["delay"]
    between each send, then disconnects.  Returns True if the account started.

    This is the unit of work that ADVANCED mode runs in parallel across all
    accounts.  The sequential per-group delay inside this function is what
    keeps each account's send rate inside Telegram's comfortable window.

    FloodWait handling
    ------------------
    Small waits  → absorbed by send_to_group (slowmode, per-chat limit).
    Large waits  → _AccountFloodWait is caught here:
                   • cooldown timestamp recorded for this account
                   • round cursor advanced past the flooded chat
                   • function returns; other parallel accounts unaffected

    Starvation prevention
    ---------------------
    Dialogs are collected once and rotated by _ROUND_CURSOR so that a
    repeatedly-flooding first chat doesn't starve later chats across rounds.
    """
    acc_key = (user_id, index)

    # Skip if still cooling down from a previous large FloodWait.
    cooldown_until = _ACCOUNT_COOLDOWN.get(acc_key, 0.0)
    if time.monotonic() < cooldown_until:
        remaining = int(cooldown_until - time.monotonic())
        logger.info(f"[acc {index + 1}] cooling down ~{remaining}s — skipped.")
        return False

    started = False
    user_app = Client(
        f"User_{user_id}_Acc_{index}",
        session_string=session,
        api_id=API_ID,
        api_hash=API_HASH,
        in_memory=True,
        no_updates=True,
        device_model="PC 64bit",
        system_version="Windows 11",
        app_version="4.16.3",
    )

    try:
        await user_app.start()
        started = True

        # Branding / join enforcement (rate-limited to ENFORCE_INTERVAL).
        now = time.monotonic()
        enforce_key = (user_id, index)
        if now - _LAST_ENFORCED.get(enforce_key, 0) >= ENFORCE_INTERVAL:
            try:
                await enforce_account(user_app, do_join=True)
            except Exception as e:
                logger.warning(f"[acc {index + 1}] enforce failed: {e}")
            finally:
                _LAST_ENFORCED[enforce_key] = time.monotonic()

        # Collect all eligible groups / supergroups for this account.
        dialogs = []
        async for dialog in user_app.get_dialogs():
            if dialog.chat and dialog.chat.type.name in ["GROUP", "SUPERGROUP"]:
                if dialog.chat.id not in VEXORA_CHAT_IDS:
                    dialogs.append(dialog)

        if dialogs:
            n = len(dialogs)
            cursor = _ROUND_CURSOR.get(acc_key, 0) % n
            # Rotate so every round starts from where the last one left off.
            ordered = dialogs[cursor:] + dialogs[:cursor]
            chats_done = 0

            for dialog in ordered:
                # Pause: block here (don't drop the round) until resumed.
                while state.get("status") == "PAUSED":
                    await asyncio.sleep(1)
                if state["status"] != "RUNNING":
                    break

                try:
                    await send_to_group(user_app, dialog.chat.id, message, state)
                    chats_done += 1
                    await asyncio.sleep(state["delay"])   # configured inter-send gap
                except _AccountFloodWait as fw:
                    logger.warning(
                        f"[acc {index + 1}] account-level FloodWait {fw.wait_seconds}s — "
                        f"round ending, cooldown set."
                    )
                    _ACCOUNT_COOLDOWN[acc_key] = time.monotonic() + fw.wait_seconds + 2
                    # Advance cursor past the flooded chat so the next round
                    # starts with the chat after it, not the same flooded one.
                    _ROUND_CURSOR[acc_key] = (cursor + chats_done + 1) % n
                    break
            # No flood → cursor stays; all groups were served this round.

        return True

    except (ConnectionError, TimeoutError, OSError) as e:
        logger.error(f"[acc {index + 1}] network error: {e}")
        await send_user_update(
            user_id, f"⚠️ **Account {index + 1}:** Connection error. Please try again."
        )
        return False
    except Exception as e:
        logger.error(f"[acc {index + 1}] error: {e}")
        await send_user_update(
            user_id, f"⚠️ **Account {index + 1}:** Session expired or logged out."
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

    NORMAL mode
    -----------
    Accounts execute one after another (sequential).  Simple and predictable.

    ADVANCED mode
    -------------
    ALL accounts run simultaneously via asyncio.gather — account 2 does not
    wait for account 1 to finish.  Within each account, groups are still sent
    sequentially with state["delay"] between sends so the per-account send rate
    stays inside Telegram's comfortable window (no burst, no FloodWait).

    Returns True if at least one account started successfully.
    """
    message = message_override or state["message"]
    current_mode = state.get("sending_mode", "NORMAL")
    sessions = list(state["sessions"])

    if not sessions:
        return False

    # Honour pause / stop before starting.
    while state.get("status") == "PAUSED":
        await asyncio.sleep(1)
    if state["status"] != "RUNNING":
        return False

    if current_mode == "ADVANCED":
        # ── All accounts in parallel ──────────────────────────────────────────
        # Each _run_account_round is an independent coroutine: account 1's delay
        # sleep does NOT block account 2.  asyncio switches between them freely.
        results = await asyncio.gather(
            *[
                _run_account_round(user_id, index, session, state, message)
                for index, session in enumerate(sessions)
            ],
            return_exceptions=True,
        )
        return any(r is True for r in results)

    else:
        # ── Accounts one at a time (NORMAL) ───────────────────────────────────
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

async def sleep_with_backoff(attempt: int, base: float = 5.0, cap: float = 300.0):
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
                await asyncio.sleep(2)   # brief pause between full rounds

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
