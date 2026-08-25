"""Broadcast workers and per-account sending logic.

Speed model (the safe, sustainable one):
  * Each account runs as its own async worker; ALL accounts run concurrently —
    this is where real throughput comes from.
  * WITHIN an account we send ONE message at a time, spaced by an adaptive
    delay (Pacer). Concurrency > 1 per account is exactly what makes Telegram
    slap the account with a multi-minute global FloodWait, so we never do it.
  * On FloodWait we wait the FULL time Telegram asks for (this account only)
    and slow the pacer down; after a clean streak the pacer speeds back up.
    No fixed 15s sleep — the pacer self-tunes to the fastest safe rate.

All send failures go through error_classifier.classify() -> one Decision:
  RETRY        transient, limited retries
  BACKOFF      FloodWait/slowmode — wait wait_seconds, retry same chat
  SKIP_GROUP   remember (account, chat) ban forever, drop the chat
  DISABLE_ACCT frozen/banned/auth-dead — tear the account down, notify once
"""
import asyncio
import time

from pyrogram import Client

from config import API_ID, API_HASH, logger
from state import USER_STATES, RUNNING_TASKS, VEXORA_CHAT_IDS
from ui import send_user_update
from enforcer import enforce_account
from error_classifier import classify, Action
import bans

# How often (seconds) to re-run branding/join enforcement on a live client.
ENFORCE_INTERVAL = 300  # 5 minutes (kept infrequent to avoid join FloodWaits)

# Tracks last enforcement time per (user_id, account_id) on THIS process.
_LAST_ENFORCED: dict[tuple[int, int], float] = {}

# Per-account pacer cache so a flood-learned slowdown survives across cycles.
_PACERS: dict[tuple[int, int], "Pacer"] = {}

# Adaptive pacing (seconds between sends, per account).
_PACE_BASE = 3.0     # start ~1 msg / 3s per account
_PACE_FLOOR = 2.0    # never go faster than this
_PACE_CEIL = 60.0    # never crawl slower than this
_SPEEDUP_AFTER = 5   # clean sends before we speed up a notch
_MAX_TRANSIENT_RETRIES = 2
_MAX_FLOOD_RETRIES = 3   # give up on a chat after this many FloodWaits in a row


class Pacer:
    """Per-account adaptive inter-send delay. Slows on flood, speeds when clean."""

    def __init__(self, base=_PACE_BASE, floor=_PACE_FLOOR, ceil=_PACE_CEIL):
        self.delay = base
        self.floor = floor
        self.ceil = ceil
        self._ok = 0

    def on_ok(self):
        self._ok += 1
        if self._ok >= _SPEEDUP_AFTER:
            self.delay = max(self.floor, self.delay * 0.8)
            self._ok = 0

    def on_flood(self):
        self._ok = 0
        self.delay = min(self.ceil, self.delay * 1.5)


async def _account_broadcast(user_id: int, index: int, session: str, state: dict, message: str) -> str:
    """One broadcast pass for a single account. Returns 'ok'|'disabled'|'idle'."""
    # Skip a known-dead session up front — no reconnect, pull it from rotation.
    if bans.is_session_disabled(session):
        try:
            state["sessions"].remove(session)
        except ValueError:
            pass
        return "disabled"

    client = Client(
        f"User_{user_id}_Acc_{index}",
        session_string=session,
        api_id=API_ID, api_hash=API_HASH,
        in_memory=True, no_updates=True,
        device_model="PC 64bit", system_version="Windows 11", app_version="4.16.3",
    )
    started = False
    account_id = None
    disable_reason: str | None = None
    sent_any = False
    try:
        await client.start()
        started = True

        me = await client.get_me()
        account_id = me.id

        # Self-heal: another session of an already-disabled account — pull it.
        if bans.is_account_disabled(account_id):
            disable_reason = "already_disabled"

        # Periodic branding enforcement on this live client (infrequent).
        if not disable_reason:
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
        if not disable_reason:
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

        if not disable_reason and not targets:
            return "idle"

        pacer = _PACERS.setdefault((user_id, account_id), Pacer())
        # Sequential, adaptively paced sends — one at a time for THIS account.
        for i, chat_id in enumerate(targets):
            if state["status"] != "RUNNING" or disable_reason:
                break

            floods = 0
            attempts = 0
            while True:
                if state["status"] != "RUNNING":
                    break
                try:
                    await client.send_message(chat_id, message)
                    state["sent_count"] += 1
                    sent_any = True
                    pacer.on_ok()
                    if i < len(targets) - 1:               # no wasted delay after the last chat
                        await asyncio.sleep(pacer.delay)
                    break
                except Exception as e:
                    dec = classify(e)
                    if dec.action == Action.BACKOFF:
                        floods += 1
                        pacer.on_flood()
                        logger.info(
                            f"[send] FloodWait {dec.wait_seconds}s acc={account_id} "
                            f"chat={chat_id} (pace now {pacer.delay:.1f}s)"
                        )
                        if floods > _MAX_FLOOD_RETRIES:
                            # This chat keeps flooding — leave it for next cycle.
                            state["failed_count"] += 1
                            break
                        # Honor Telegram's wait FULLY for this account, then retry.
                        await asyncio.sleep(dec.wait_seconds + 1)
                        continue
                    if dec.action == Action.SKIP_GROUP:
                        await bans.record_group_ban(account_id, chat_id, dec.reason)
                        break
                    if dec.action == Action.DISABLE_ACCT:
                        disable_reason = dec.reason
                        break
                    # RETRY — transient
                    attempts += 1
                    if attempts > _MAX_TRANSIENT_RETRIES:
                        state["failed_count"] += 1
                        logger.warning(f"[send] giving up chat={chat_id} acc={account_id}: {e}")
                        break
                    await asyncio.sleep(2 ** attempts)

            if disable_reason:
                break

    except asyncio.CancelledError:
        raise
    except Exception as e:
        dec = classify(e)
        if dec.action == Action.DISABLE_ACCT:
            disable_reason = dec.reason
        elif dec.action == Action.BACKOFF:
            # FloodWait during start()/get_dialogs — wait it out so we don't
            # reconnect-spam this account while Telegram is cooling it down.
            logger.info(f"[account] setup FloodWait {dec.wait_seconds}s acc={account_id or index+1}")
            await asyncio.sleep(dec.wait_seconds + 1)
        else:
            logger.error(f"[account] acc {index+1} (id={account_id}) error: {e}")
            await send_user_update(user_id, f"⚠️ **Account {index+1}:** connection error. Retrying next cycle.")
    finally:
        if started:
            try:
                await client.stop()
            except Exception as e:
                logger.debug(f"[account] stop() error acc={account_id}: {e}")

    if disable_reason:
        await bans.disable_account(session, account_id, disable_reason)
        try:
            state["sessions"].remove(session)
        except ValueError:
            pass
        await bans.notify_admin_once(session, account_id, disable_reason, user_id)
        await send_user_update(
            user_id,
            f"🛑 **Account {index+1} auto-removed:** {disable_reason}. It won't be used again.",
        )
        return "disabled"

    return "ok" if sent_any else "idle"


async def broadcast_once(user_id: int, state: dict, message_override: str | None = None) -> bool:
    """Run every account's broadcast pass concurrently (one pass each).

    Returns True if at least one account sent at least one message this pass.
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

    No fixed inter-cycle sleep. It idles (growing backoff) only when a whole
    cycle sent nothing — e.g. every account is in a FloodWait cooldown or every
    group is banned — so it doesn't reconnect-spam. Productive cycles chain
    back-to-back at the pacer's self-tuned safe rate.
    """
    idle_backoff = 5
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
                idle_backoff = 5
            else:
                await asyncio.sleep(idle_backoff)
                idle_backoff = min(idle_backoff * 2, 120)
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
