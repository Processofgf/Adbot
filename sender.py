"""Broadcast workers and per-account sending logic.

Every account runs as its own concurrent async worker. Sends inside an account
are bounded by an adaptive semaphore that shrinks on FloodWait (this account
only) and grows back after a clean streak — self-tuning to the fastest rate
Telegram tolerates, with no fixed sleep between cycles.

All send failures go through error_classifier.classify() → a single Decision:
  RETRY        transient, limited retries
  BACKOFF      FloodWait/slowmode — pause THIS account, retry same chat
  SKIP_GROUP   remember (account, chat) ban forever, drop the chat
  DISABLE_ACCT frozen/banned/auth-dead — tear the account down, notify once
"""
import asyncio
import time

from pyrogram import Client

from config import API_ID, API_HASH, ADMIN_ID, logger
from state import USER_STATES, RUNNING_TASKS, VEXORA_CHAT_IDS
from ui import send_user_update
from enforcer import enforce_account
from error_classifier import classify, Action
import bans

# How often (seconds) to re-run the branding/join/leave-check enforcement
# pass on an account that's actively broadcasting.
ENFORCE_INTERVAL = 120  # 2 minutes

# Tracks last enforcement time per (user_id, account_id) on THIS process.
_LAST_ENFORCED: dict[tuple[int, int], float] = {}

# Adaptive controller tuning.
_SEM_START = 5      # start at 5 concurrent sends per account
_SEM_FLOOR = 1
_SEM_CEIL = 15
_GROW_AFTER = 20    # clean sends before growing the limit by 1
_MAX_TRANSIENT_RETRIES = 2


class AdaptiveLimiter:
    """Per-account concurrency limiter that reacts to FloodWait.

    * acquire() blocks while at the limit AND while a flood pause is active.
    * on_success() grows the limit after a clean streak (up to ceiling).
    * on_flood(secs) halves the limit and pauses this account for `secs`.
    """

    def __init__(self, start=_SEM_START, floor=_SEM_FLOOR, ceiling=_SEM_CEIL,
                 grow_after=_GROW_AFTER):
        self.limit = start
        self.floor = floor
        self.ceiling = ceiling
        self.grow_after = grow_after
        self._in_flight = 0
        self._pause_until = 0.0
        self._ok = 0
        self._cond = asyncio.Condition()

    async def acquire(self):
        # Respect a flood pause first (released lock while sleeping).
        while True:
            now = time.monotonic()
            if now < self._pause_until:
                await asyncio.sleep(self._pause_until - now)
                continue
            break
        async with self._cond:
            while self._in_flight >= self.limit:
                await self._cond.wait()
            self._in_flight += 1

    async def release(self):
        async with self._cond:
            self._in_flight -= 1
            self._cond.notify(1)

    async def on_success(self):
        async with self._cond:
            self._ok += 1
            if self._ok >= self.grow_after and self.limit < self.ceiling:
                self.limit += 1
                self._ok = 0
                self._cond.notify(1)

    async def on_flood(self, wait_seconds: int):
        async with self._cond:
            self._pause_until = time.monotonic() + max(1, wait_seconds)
            self._ok = 0
            if self.limit > self.floor:
                self.limit = max(self.floor, self.limit // 2)


async def _send_one(client, chat_id, message, state, limiter, account_id, disable_evt, disable_reason):
    """Send to one chat, handling its own retries/flood/ban in place.

    Returns True if a message was actually sent (for cycle idle-detection).
    """
    attempts = 0
    while state["status"] == "RUNNING" and not disable_evt.is_set():
        await limiter.acquire()
        try:
            await client.send_message(chat_id, message)
            state["sent_count"] += 1
            await limiter.on_success()
            return True
        except Exception as e:
            dec = classify(e)
            if dec.action == Action.BACKOFF:
                logger.info(f"[send] FloodWait {dec.wait_seconds}s acc={account_id} chat={chat_id}")
                await limiter.on_flood(dec.wait_seconds)
                continue  # retry same chat after the pause
            if dec.action == Action.SKIP_GROUP:
                await bans.record_group_ban(account_id, chat_id, dec.reason)
                return False
            if dec.action == Action.DISABLE_ACCT:
                disable_reason["reason"] = dec.reason
                disable_evt.set()
                return False
            # RETRY — transient
            attempts += 1
            if attempts > _MAX_TRANSIENT_RETRIES:
                state["failed_count"] += 1
                logger.warning(f"[send] giving up chat={chat_id} acc={account_id}: {e}")
                return False
            await asyncio.sleep(2 ** attempts)
        finally:
            await limiter.release()
    return False


async def _account_broadcast(user_id: int, index: int, session: str, state: dict, message: str) -> str:
    """Run one broadcast pass for a single account. Returns 'ok'|'disabled'|'idle'."""
    client = Client(
        f"User_{user_id}_Acc_{index}",
        session_string=session,
        api_id=API_ID, api_hash=API_HASH,
        in_memory=True, no_updates=True,
        device_model="PC 64bit", system_version="Windows 11", app_version="4.16.3",
    )
    started = False
    account_id = None
    disable_evt = asyncio.Event()
    disable_reason: dict = {}
    sent_any = False
    try:
        await client.start()
        started = True

        me = await client.get_me()
        account_id = me.id

        # Self-heal: session for an already-disabled account — pull it now.
        if bans.is_account_disabled(account_id):
            disable_reason["reason"] = "already_disabled"
            disable_evt.set()
            return "disabled"

        # Periodic branding enforcement on this live client.
        enforce_key = (user_id, account_id)
        now = time.monotonic()
        if now - _LAST_ENFORCED.get(enforce_key, 0) >= ENFORCE_INTERVAL:
            try:
                await enforce_account(client, do_join=True)
            except Exception as e:
                logger.warning(f"[account] enforce failed acc={account_id}: {e}")
            finally:
                _LAST_ENFORCED[enforce_key] = now

        # Build the target list, skipping brand channels + remembered bans.
        targets: list[int] = []
        async for dialog in client.get_dialogs():
            if state["status"] != "RUNNING":
                break
            chat = dialog.chat
            if not chat or chat.type.name not in ("GROUP", "SUPERGROUP"):
                continue
            if chat.id in VEXORA_CHAT_IDS:
                continue
            if bans.is_group_banned(account_id, chat.id):
                continue
            targets.append(chat.id)

        if not targets:
            return "idle"

        # Adaptive concurrent dispatch: a bounded pool of workers pulls chats
        # from a queue; each worker gates real concurrency through the limiter.
        limiter = AdaptiveLimiter()
        queue: asyncio.Queue = asyncio.Queue()
        for cid in targets:
            queue.put_nowait(cid)

        async def worker():
            nonlocal sent_any
            while state["status"] == "RUNNING" and not disable_evt.is_set():
                try:
                    cid = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                ok = await _send_one(client, cid, message, state, limiter,
                                     account_id, disable_evt, disable_reason)
                if ok:
                    sent_any = True

        pool = [asyncio.create_task(worker()) for _ in range(_SEM_CEIL)]
        await asyncio.gather(*pool)

    except asyncio.CancelledError:
        raise
    except Exception as e:
        dec = classify(e)
        if dec.action == Action.DISABLE_ACCT:
            disable_reason["reason"] = dec.reason
            disable_evt.set()
        else:
            logger.error(f"[account] acc {index+1} (id={account_id}) error: {e}")
            await send_user_update(user_id, f"⚠️ **Account {index+1}:** connection error. Retrying next cycle.")
    finally:
        if started:
            try:
                await client.stop()
            except Exception as e:
                logger.debug(f"[account] stop() error acc={account_id}: {e}")

    if disable_evt.is_set():
        reason = disable_reason.get("reason", "account_frozen_or_banned")
        await bans.disable_account(account_id, reason)
        # Pull the session out of rotation for good (safe remove-by-value).
        try:
            state["sessions"].remove(session)
        except ValueError:
            pass
        await bans.notify_admin_once(account_id, reason, user_id)
        await send_user_update(
            user_id,
            f"🛑 **Account {index+1} auto-removed:** {reason}. It won't be used again.",
        )
        return "disabled"

    return "ok" if sent_any else "idle"


async def broadcast_once(user_id: int, state: dict, message_override: str | None = None) -> bool:
    """Run every account's broadcast pass concurrently (one pass each).

    Returns True if at least one account ran and sent at least one message.
    """
    message = message_override or state["message"]
    if state["status"] not in ("RUNNING", "PAUSED"):
        return False
    while state.get("status") == "PAUSED":
        await asyncio.sleep(1)
    if state["status"] != "RUNNING":
        return False

    sessions = list(state.get("sessions", []))
    if not sessions:
        return False

    tasks = [
        asyncio.create_task(_account_broadcast(user_id, i, s, state, message))
        for i, s in enumerate(sessions)
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    for r in results:
        if isinstance(r, Exception) and not isinstance(r, asyncio.CancelledError):
            logger.error(f"[broadcast_once] account task crashed: {r}")
    return any(r == "ok" for r in results if not isinstance(r, Exception))


async def dedicated_user_worker(user_id: int):
    """Main loop for manual RUN. Keeps broadcasting until STOPPED.

    No fixed inter-cycle sleep. It only idles briefly when a whole cycle had
    nothing to send (e.g. every group already banned) to avoid busy-spinning
    on get_dialogs; active cycles chain back-to-back at Telegram's max safe rate.
    """
    idle_backoff = 2
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

            sent_something = await broadcast_once(user_id, state)
            if state["status"] != "RUNNING":
                break

            if sent_something:
                idle_backoff = 2  # reset; go straight into the next cycle
            else:
                # Nothing sent this cycle — back off a little (cap 60s) so we
                # don't hammer get_dialogs when every group is banned/idle.
                await asyncio.sleep(idle_backoff)
                idle_backoff = min(idle_backoff * 2, 60)
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
        if state["status"] == "RUNNING":
            state["status"] = "STOPPED"
    await send_user_update(user_id, "✅ **Scheduled broadcast finished.**")
