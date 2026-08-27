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

# How often (seconds) to re-run branding/join enforcement on an actively
# broadcasting account's already-started client.
ENFORCE_INTERVAL = 120  # 2 minutes

# Tracks last enforcement time per (user_id, session_index) on THIS process.
_LAST_ENFORCED: dict[tuple[int, int], float] = {}

# ── FloodWait policy ────────────────────────────────────────────────────────
# Telegram issues FloodWait for two distinct reasons:
#   • Per-chat slowmode / send-rate limit  →  small, usually ≤ 30 s.
#   • Account-level rate-limiting          →  large, 100–3600 s in the logs.
#
# Old behaviour: sleep the full wait inline, then retry the same chat up to 3×.
# That burned up to ~15 minutes on one flooded account before moving on, and
# re-flooded on the next chat too — producing the "sends a bit, dead 6 min,
# repeat" pattern visible in the logs.
#
# New behaviour:
#   • Small (≤ LARGE_FLOOD_THRESHOLD)  →  sleep + retry inline (unchanged).
#     Handles per-chat slowmode correctly.
#   • Large (>  LARGE_FLOOD_THRESHOLD) →  raise _AccountFloodWait immediately.
#     broadcast_once catches this, records a per-account cooldown timestamp,
#     advances the round cursor past the flooded chat, and moves on — no inline
#     sleep, other accounts keep going.
# ────────────────────────────────────────────────────────────────────────────
LARGE_FLOOD_THRESHOLD = 30  # seconds

# ── ADVANCED mode concurrency ────────────────────────────────────────────────
# Old ADVANCED behaviour: gather() fired ALL group sends simultaneously → burst
# → Telegram issued 250–325 s FloodWait on every account.
#
# New behaviour: tasks are still created concurrently (faster than NORMAL) but
# launches are staggered so sends spread out over time.
#
#   stagger_delay = state["delay"] / ADVANCED_CONCURRENCY
#
# With delay=15 and ADVANCED_CONCURRENCY=5: one new task every 3 s, so
# ADVANCED is 5× faster than NORMAL (which waits the full 15 s after each send)
# while the per-account request rate stays inside Telegram's comfortable window.
#
# Adjust ADVANCED_CONCURRENCY higher for more speed / lower for more safety.
ADVANCED_CONCURRENCY = 5   # parallel task "lanes" per account per round
ADVANCED_MIN_STAGGER = 0.5 # floor: never launch two tasks faster than this

# Per (user_id, session_index) → monotonic time when the account may resume.
_ACCOUNT_COOLDOWN: dict[tuple[int, int], float] = {}

# ── Rotating round cursor ───────────────────────────────────────────────────
# Without a cursor every round restarts at dialogs[0].  If that chat keeps
# flooding, the round ends there every time and later chats are starved.
#
# _ROUND_CURSOR stores, per account, the dialog-list index where the *next*
# round should begin.  On a large FloodWait the cursor advances past the
# flooded chat so the next round starts with the chat immediately after it.
# After a clean full round (no flood) all groups were served anyway, so the
# cursor stays where it is — it will naturally advance again on the next flood.
# ────────────────────────────────────────────────────────────────────────────
_ROUND_CURSOR: dict[tuple[int, int], int] = {}


class _AccountFloodWait(Exception):
    """Internal signal: Telegram returned a large (account-level) FloodWait.

    Raised by send_to_group and caught by broadcast_once to end that account's
    round cleanly without sleeping inline for hundreds of seconds.  The caller:
      1. Records _ACCOUNT_COOLDOWN so the account is skipped on subsequent
         rounds until Telegram's window expires.
      2. Advances _ROUND_CURSOR past the flooded chat.
      3. Moves on to the next account — no sleep here.
    """

    def __init__(self, wait_seconds: int):
        self.wait_seconds = wait_seconds
        super().__init__(f"AccountFloodWait({wait_seconds}s)")


async def send_to_group(
    user_app, chat_id: int, message: str, state: dict, max_net_retries: int = 2
):
    """Send one message to a chat with FloodWait + limited network-retry handling.

    Small FloodWait (≤ LARGE_FLOOD_THRESHOLD) is absorbed inline so per-chat
    slowmode still gets honoured.  Large FloodWait raises _AccountFloodWait so
    broadcast_once can end that account's round without a 100–3600 s inline sleep.
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
            logger.info(f"[send_to_group] FloodWait {wait}s for chat {chat_id}")
            if wait <= LARGE_FLOOD_THRESHOLD:
                # Per-chat slowmode — absorb inline and retry once.
                await asyncio.sleep(wait + 1)
            else:
                # Account-level rate-limit — signal caller to end this account's round.
                raise _AccountFloodWait(wait)
        except (ConnectionError, TimeoutError, OSError) as e:
            net_attempt += 1
            if net_attempt > max_net_retries:
                logger.warning(
                    f"[send_to_group] Network error chat {chat_id} after {net_attempt} tries: {e}"
                )
                state["failed_count"] += 1
                return
            backoff = 2 ** net_attempt
            logger.debug(
                f"[send_to_group] Network retry {net_attempt}/{max_net_retries} in {backoff}s: {e}"
            )
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


async def broadcast_once(
    user_id: int, state: dict, message_override: str | None = None
) -> bool:
    """Iterate every account once and send the promo message to all groups.

    Returns True if at least one account started successfully.
    Respects PAUSED (blocks) / STOPPED (early exit) status changes.

    FloodWait handling
    ------------------
    Per-chat waits ≤ LARGE_FLOOD_THRESHOLD are absorbed inside send_to_group.
    Larger waits are caught here as _AccountFloodWait:
      • NORMAL mode: account's dialog loop ends immediately, _ACCOUNT_COOLDOWN
        is set, cursor advances past the flooded chat.  No inline sleep.
      • ADVANCED mode: per-task — the individual task silently skips that chat
        and increments failed_count.  Other concurrent tasks keep running.
      • Other accounts in the same round continue without interruption.

    This also fixes the scheduler stall: broadcast_once now returns quickly
    even when every account is rate-limited (no 300 s inline sleep).

    ADVANCED mode — staggered sends (flood prevention)
    ---------------------------------------------------
    Old: gather() fired every group simultaneously → burst → Telegram issued
    250–325 s FloodWait on every account.

    New: tasks are created sequentially, each launch separated by
    stagger_delay = delay / ADVANCED_CONCURRENCY.  Tasks then run concurrently
    so ADVANCED is still ADVANCED_CONCURRENCY× faster than NORMAL, but
    Telegram sees a steady drip rather than a burst.

    Starvation prevention — both modes
    ------------------------------------
    _ROUND_CURSOR stores, per account, the dialog-list index where the next
    round begins.  When a chat floods in NORMAL mode, the cursor moves past it
    so the next round starts with the following chat.  ADVANCED mode also
    collects and rotates dialogs, so it benefits from the same fairness.
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

        # ── Account-level cooldown check ─────────────────────────────────────
        acc_key = (user_id, index)
        cooldown_until = _ACCOUNT_COOLDOWN.get(acc_key, 0.0)
        if time.monotonic() < cooldown_until:
            remaining = int(cooldown_until - time.monotonic())
            logger.info(
                f"[broadcast_once] acc {index + 1} cooling down for ~{remaining}s — skipping."
            )
            continue

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

            # ── In-broadcast branding enforcement ────────────────────────────
            # Re-run name/bio/channel enforcement every ENFORCE_INTERVAL seconds
            # on this already-started client, catching accounts that left a
            # Vexora channel or had profile changes while broadcasting.
            enforce_key = (user_id, index)
            now = time.monotonic()
            if now - _LAST_ENFORCED.get(enforce_key, 0) >= ENFORCE_INTERVAL:
                try:
                    await enforce_account(user_app, do_join=True)
                except Exception as e:
                    logger.warning(
                        f"[broadcast_once] pre-send enforce failed acc {index + 1}: {e}"
                    )
                finally:
                    _LAST_ENFORCED[enforce_key] = now

            if current_mode == "ADVANCED":
                # ── Staggered-parallel (ADVANCED) mode ───────────────────────
                # Collect eligible dialogs and apply the same rotating cursor as
                # NORMAL mode so early-flooding chats don't starve later ones.
                dialogs = []
                async for dialog in user_app.get_dialogs():
                    if dialog.chat and dialog.chat.type.name in ["GROUP", "SUPERGROUP"]:
                        if dialog.chat.id not in VEXORA_CHAT_IDS:
                            dialogs.append(dialog)

                if dialogs:
                    n = len(dialogs)
                    cursor = _ROUND_CURSOR.get(acc_key, 0) % n
                    ordered = dialogs[cursor:] + dialogs[:cursor]

                    # stagger_delay spaces out task LAUNCHES so sends arrive at
                    # Telegram as a steady stream, not a simultaneous burst.
                    # = delay/ADVANCED_CONCURRENCY → ADVANCED_CONCURRENCY× faster
                    #   than NORMAL mode, same total per-account message rate.
                    stagger_delay = max(
                        state["delay"] / ADVANCED_CONCURRENCY,
                        ADVANCED_MIN_STAGGER,
                    )

                    # Inner coroutine: send + absorb per-task flood silently.
                    # We don't set an account-level cooldown here because other
                    # tasks for this account are already in flight — one flooded
                    # chat should not abort the whole concurrent batch.
                    async def _safe_send(chat_id: int):
                        try:
                            await send_to_group(user_app, chat_id, message, state)
                        except _AccountFloodWait as fw:
                            state["failed_count"] += 1
                            logger.info(
                                f"[advanced] acc {index + 1} chat {chat_id} "
                                f"flood {fw.wait_seconds}s — skipping this chat."
                            )

                    launched: list[asyncio.Task] = []
                    for dialog in ordered:
                        if state["status"] != "RUNNING":
                            # Cancel already-launched tasks cleanly.
                            for t in launched:
                                t.cancel()
                            break
                        launched.append(
                            asyncio.create_task(_safe_send(dialog.chat.id))
                        )
                        # Stagger: yield to the event loop long enough for the
                        # previous task to start its network call before we
                        # queue the next one — spreading requests over time.
                        await asyncio.sleep(stagger_delay)

                    if launched:
                        await asyncio.gather(*launched, return_exceptions=True)

            else:
                # ── Sequential (NORMAL) mode with rotating cursor ─────────────
                # Collect all eligible dialogs first so we can slice from any
                # offset without re-consuming the async generator.
                dialogs = []
                async for dialog in user_app.get_dialogs():
                    if dialog.chat and dialog.chat.type.name in ["GROUP", "SUPERGROUP"]:
                        if dialog.chat.id not in VEXORA_CHAT_IDS:
                            dialogs.append(dialog)

                if dialogs:
                    n = len(dialogs)
                    # Guard against stale cursors if the dialog count changed.
                    cursor = _ROUND_CURSOR.get(acc_key, 0) % n
                    # Rotate: start at cursor, wrap around to cover all dialogs.
                    ordered = dialogs[cursor:] + dialogs[:cursor]
                    chats_done = 0

                    for dialog in ordered:
                        if state["status"] != "RUNNING":
                            break
                        try:
                            await send_to_group(
                                user_app, dialog.chat.id, message, state
                            )
                            chats_done += 1
                            await asyncio.sleep(state["delay"])
                        except _AccountFloodWait as fw:
                            logger.warning(
                                f"[broadcast_once] acc {index + 1} account-level "
                                f"FloodWait {fw.wait_seconds}s — "
                                f"ending round, cooldown recorded."
                            )
                            # Record cooldown so next broadcast_once call skips
                            # this account until Telegram's window is over.
                            _ACCOUNT_COOLDOWN[acc_key] = (
                                time.monotonic() + fw.wait_seconds + 2
                            )
                            # Advance past the flooded chat.
                            # ordered[chats_done] is the flooded entry;
                            # (cursor + chats_done + 1) % n is the chat after it.
                            _ROUND_CURSOR[acc_key] = (cursor + chats_done + 1) % n
                            break
                    # If no flood occurred, cursor stays — all groups were served
                    # and any starting point is equally good next round.

        except (ConnectionError, TimeoutError, OSError) as e:
            logger.error(f"[broadcast_once] Network error acc {index + 1}: {e}")
            await send_user_update(
                user_id,
                f"⚠️ **Account {index + 1}:** Direct connection error. Please try again.",
            )
        except Exception as e:
            logger.error(f"[broadcast_once] Account {index + 1} error: {e}")
            await send_user_update(
                user_id,
                f"⚠️ **Account {index + 1}:** Session expired or logged out.",
            )
        finally:
            if started:
                try:
                    await user_app.stop()
                except Exception as e:
                    logger.debug(
                        f"[broadcast_once] stop() error acc {index + 1}: {e}"
                    )

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
                    f"[Worker] All accounts failed (attempt {consecutive_failures}). Backing off..."
                )
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
        logger.error(
            f"[Worker] Unhandled exception for user {user_id}: {e}", exc_info=True
        )
    finally:
        RUNNING_TASKS.pop(user_id, None)
        logger.info(f"[Worker] Task ended for user {user_id}")


async def run_scheduled_broadcast(
    user_id: int, message_override: str | None = None
):
    """One-shot broadcast triggered by the scheduler.

    broadcast_once now returns quickly even when every account is flooded
    (no inline sleep), so the scheduler's awaited call no longer stalls the
    whole scheduled pass.
    """
    state = USER_STATES.get(user_id)
    if not state:
        return
    if state["status"] == "RUNNING":
        logger.info(
            f"[Scheduler] User {user_id} already RUNNING; skipping scheduled broadcast."
        )
        return
    if not state["sessions"]:
        await send_user_update(
            user_id, "⚠️ **Scheduled broadcast skipped:** No active accounts."
        )
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
