"""Broadcast worker and per-account sending logic."""
import asyncio
import random

from pyrogram import Client
from pyrogram.errors import FloodWait, RPCError

import time

from config import API_ID, API_HASH, logger
from state import USER_STATES, RUNNING_TASKS, VEXORA_CHAT_IDS
from ui import send_user_update
from enforcer import enforce_account

# How often (seconds) to re-run the branding/join/leave-check enforcement
# pass on an account that's actively broadcasting. Uses the account's own
# already-started client (no parallel-client conflict) so it can run even
# while status == RUNNING, unlike the background enforcer_loop which skips
# RUNNING accounts entirely.
ENFORCE_INTERVAL = 120  # 2 minutes

# Tracks last enforcement time per (user_id, session_index) on THIS process.
_LAST_ENFORCED: dict[tuple[int, int], float] = {}


async def send_to_group(user_app, chat_id: int, message: str, state: dict, max_net_retries: int = 2):
    """Send one message to a chat with FloodWait + limited network retry handling."""
    net_attempt = 0
    while True:
        if state["status"] != "RUNNING":
            return
        try:
            await user_app.send_message(chat_id, message)
            state["sent_count"] += 1
            return
        except FloodWait as e:
            logger.info(f"[send_to_group] FloodWait {e.value}s for chat {chat_id}")
            await asyncio.sleep(e.value + 1)
        except (ConnectionError, TimeoutError, OSError) as e:
            net_attempt += 1
            if net_attempt > max_net_retries:
                logger.warning(f"[send_to_group] Network error chat {chat_id} after {net_attempt} tries: {e}")
                state["failed_count"] += 1
                return
            backoff = 2 ** net_attempt
            logger.debug(f"[send_to_group] Network retry {net_attempt}/{max_net_retries} in {backoff}s: {e}")
            await asyncio.sleep(backoff)
        except RPCError as e:
            logger.info(f"[send_to_group] RPC error chat {chat_id}: {e}")
            state["failed_count"] += 1
            return
        except Exception as e:
            logger.error(f"[send_to_group] Unexpected error chat {chat_id}: {e}")
            state["failed_count"] += 1
            return


async def sleep_with_backoff(attempt: int, base: float = 5.0, cap: float = 300.0):
    delay = min(base * (2 ** attempt), cap)
    jitter = random.uniform(0, delay * 0.15)
    await asyncio.sleep(delay + jitter)


async def _maybe_enforce(user_app, user_id: int, index: int) -> None:
    """Run enforcement on this live client if ENFORCE_INTERVAL has elapsed
    since the last pass — called both before AND during the send loop, so a
    single account with many groups (long-running dialog loop) still gets
    re-checked every ~2 minutes instead of only once at the start."""
    enforce_key = (user_id, index)
    now = time.monotonic()
    if now - _LAST_ENFORCED.get(enforce_key, float("-inf")) < ENFORCE_INTERVAL:
        return
    try:
        await enforce_account(user_app, do_join=True)
    except Exception as e:
        logger.warning(f"[broadcast_once] enforce failed acc {index+1}: {e}")
    finally:
        _LAST_ENFORCED[enforce_key] = now


async def broadcast_once(user_id: int, state: dict, message_override: str | None = None) -> bool:
    """Iterate every account once and send the promo message to all groups.

    Returns True if at least one account started successfully.
    Respects PAUSED (blocks) / STOPPED (early exit) status changes.
    """
    message = message_override or state["message"]
    current_mode = state.get("sending_mode", "NORMAL")
    had_success = False

    for index, session in enumerate(list(state["sessions"])):
        if state["status"] not in ("RUNNING", "PAUSED"):
            break
        while state.get("status") == "PAUSED":
            await asyncio.sleep(1)
        if state["status"] != "RUNNING":
            break

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
            had_success = True

            # Re-run branding + join/leave-check enforcement on THIS live
            # client every ENFORCE_INTERVAL seconds, instead of only once —
            # this is what catches an account that left a Vexora channel/
            # group, or had its name/bio changed, while it keeps broadcasting.
            await _maybe_enforce(user_app, user_id, index)

            if current_mode == "ADVANCED":
                tasks = []
                async for dialog in user_app.get_dialogs():
                    if state["status"] != "RUNNING":
                        break
                    await _maybe_enforce(user_app, user_id, index)
                    if dialog.chat and dialog.chat.type.name in ["GROUP", "SUPERGROUP"]:
                        if dialog.chat.id in VEXORA_CHAT_IDS:
                            continue  # never broadcast into brand channels
                        tasks.append(send_to_group(user_app, dialog.chat.id, message, state))
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
            else:
                async for dialog in user_app.get_dialogs():
                    if state["status"] != "RUNNING":
                        break
                    await _maybe_enforce(user_app, user_id, index)
                    if dialog.chat and dialog.chat.type.name in ["GROUP", "SUPERGROUP"]:
                        if dialog.chat.id in VEXORA_CHAT_IDS:
                            continue  # never broadcast into brand channels
                        await send_to_group(user_app, dialog.chat.id, message, state)
                        await asyncio.sleep(state["delay"])

        except (ConnectionError, TimeoutError, OSError) as e:
            logger.error(f"[broadcast_once] Network error acc {index+1}: {e}")
            await send_user_update(user_id, f"⚠️ **Account {index+1}:** Direct connection error. Please try again.")
        except Exception as e:
            logger.error(f"[broadcast_once] Account {index+1} error: {e}")
            await send_user_update(user_id, f"⚠️ **Account {index+1}:** Session expired or logged out.")
        finally:
            if started:
                try:
                    await user_app.stop()
                except Exception as e:
                    logger.debug(f"[broadcast_once] stop() error acc {index+1}: {e}")

    return had_success


async def dedicated_user_worker(user_id: int):
    """Main loop for manual RUN. Keeps broadcasting until STOPPED."""
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
                await send_user_update(user_id, "⚠️ **Engine Stopped:** No active accounts. Please add an account.")
                state["status"] = "STOPPED"
                break

            had_success = await broadcast_once(user_id, state)
            if state["status"] != "RUNNING":
                break

            if not had_success:
                consecutive_failures += 1
                logger.warning(f"[Worker] All accounts failed (attempt {consecutive_failures}). Backing off...")
                await sleep_with_backoff(consecutive_failures)
            else:
                consecutive_failures = 0
                if state.get("sending_mode", "NORMAL") == "ADVANCED":
                    await asyncio.sleep(state["delay"])
                else:
                    await asyncio.sleep(2)
    except asyncio.CancelledError:
        logger.info(f"[Worker] Task cancelled for user {user_id}")
    except Exception as e:
        logger.error(f"[Worker] Unhandled exception for user {user_id}: {e}", exc_info=True)
    finally:
        RUNNING_TASKS.pop(user_id, None)
        logger.info(f"[Worker] Task ended for user {user_id}")


async def run_scheduled_broadcast(user_id: int, message_override: str | None = None):
    """One-shot broadcast triggered by the scheduler."""
    state = USER_STATES.get(user_id)
    if not state:
        return
    if state["status"] == "RUNNING":
        logger.info(f"[Scheduler] User {user_id} already RUNNING; skipping scheduled broadcast.")
        return
    if not state["sessions"]:
        await send_user_update(user_id, "⚠️ **Scheduled broadcast skipped:** No active accounts.")
        return

    state["status"] = "RUNNING"
    await send_user_update(user_id, "🗓 **Scheduled broadcast starting...**")
    try:
        await broadcast_once(user_id, state, message_override=message_override)
    finally:
        # If it wasn't stopped/paused manually mid-run, reset to STOPPED so the
        # next schedule fires cleanly.
        if state["status"] == "RUNNING":
            state["status"] = "STOPPED"
    await send_user_update(user_id, "✅ **Scheduled broadcast finished.**")
