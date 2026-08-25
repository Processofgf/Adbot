"""Broadcast workers and per-account sending logic.

Speed / smoothness model:
  * Each account runs as its OWN INDEPENDENT perpetual worker with a persistent
    client. Accounts are fully decoupled: if Telegram hits one account with a
    multi-minute FloodWait, ONLY that account pauses — every other account keeps
    sending, so the overall stream stays continuous (no fleet-wide silence).
  * WITHIN an account we send ONE message at a time, spaced by an adaptive
    delay (Pacer). Concurrency > 1 per account is exactly what triggers the big
    global FloodWaits, so we never do it.
  * On FloodWait we wait the FULL time Telegram asks for (this account only) and
    slow that account's pacer; after a clean streak the pacer speeds back up.
    No fixed sleep — the pacer self-tunes to the fastest safe rate.

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

# Gap between full round-robin passes of an account's group list, so we don't
# instantly re-blast the same groups (which escalates FloodWaits).
_ROUND_GAP = 5.0


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


def _remove_session(state: dict, session: str):
    try:
        state["sessions"].remove(session)
    except ValueError:
        pass


def _make_client(user_id: int, index: int, session: str) -> Client:
    return Client(
        f"User_{user_id}_Acc_{index}",
        session_string=session,
        api_id=API_ID, api_hash=API_HASH,
        in_memory=True, no_updates=True,
        device_model="PC 64bit", system_version="Windows 11", app_version="4.16.3",
    )


async def _maybe_enforce(user_id: int, account_id: int, client: Client):
    key = (user_id, account_id)
    now = time.monotonic()
    if now - _LAST_ENFORCED.get(key, 0) >= ENFORCE_INTERVAL:
        try:
            await enforce_account(client, do_join=True)
        except Exception as e:
            logger.warning(f"[account] enforce failed acc={account_id}: {e}")
        finally:
            _LAST_ENFORCED[key] = now


async def _collect_targets(client: Client, account_id: int, state: dict) -> list[int]:
    """Group chat ids to broadcast to, skipping brand channels + remembered bans."""
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
    return targets


async def _send_to_chat(client, chat_id, message, state, account_id, pacer) -> tuple[str, str | None]:
    """Send to one chat, honoring flood/skip/disable in place.

    Returns (status, reason) where status is
    'ok' | 'skip' | 'failed' | 'stop' | 'disable'.
    """
    floods = 0
    attempts = 0
    while True:
        if state["status"] != "RUNNING":
            return ("stop", None)
        try:
            await client.send_message(chat_id, message)
            state["sent_count"] += 1
            pacer.on_ok()
            return ("ok", None)
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
                    state["failed_count"] += 1
                    return ("failed", None)
                await asyncio.sleep(dec.wait_seconds + 1)
                continue
            if dec.action == Action.SKIP_GROUP:
                await bans.record_group_ban(account_id, chat_id, dec.reason)
                return ("skip", None)
            if dec.action == Action.DISABLE_ACCT:
                return ("disable", dec.reason)
            attempts += 1
            if attempts > _MAX_TRANSIENT_RETRIES:
                state["failed_count"] += 1
                logger.warning(f"[send] giving up chat={chat_id} acc={account_id}: {e}")
                return ("failed", None)
            await asyncio.sleep(2 ** attempts)


async def _finalize_disable(user_id, index, session, account_id, reason, state):
    """Persist disable, pull the session, fire-once notify + user message."""
    await bans.disable_account(session, account_id, reason)
    if state is not None:
        _remove_session(state, session)
    await bans.notify_admin_once(session, account_id, reason, user_id)
    label = f"Account {index+1}" + (f" (id {account_id})" if account_id else "")
    await send_user_update(
        user_id,
        f"🛑 **{label} auto-removed:** {reason}. It won't be used again.",
    )


async def _send_targets(client, targets, message, state, account_id, pacer) -> tuple[bool, str | None]:
    """Send one round-robin pass over targets. Returns (sent_any, disable_reason)."""
    sent_any = False
    for i, chat_id in enumerate(targets):
        if state["status"] != "RUNNING":
            break
        status, reason = await _send_to_chat(client, chat_id, message, state, account_id, pacer)
        if status == "disable":
            return sent_any, reason
        if status == "stop":
            break
        if status == "ok":
            sent_any = True
        if i < len(targets) - 1:            # no wasted delay after the last chat
            await asyncio.sleep(pacer.delay)
    return sent_any, None


# ==================== INDEPENDENT PERPETUAL WORKER (manual RUN) ====================
async def _account_perpetual(user_id: int, index: int, session: str):
    """Keep ONE account broadcasting continuously until STOPPED/PAUSED.

    Persistent client (started once), round-robins its group list forever. A
    FloodWait pauses only THIS account; other accounts' workers keep running.
    Reconnects with a short backoff if the connection drops.
    """
    while True:
        state = USER_STATES.get(user_id)
        if not state or state.get("status") != "RUNNING":
            return
        if bans.is_session_disabled(session):
            _remove_session(state, session)
            return

        client = _make_client(user_id, index, session)
        started = False
        account_id = None
        disable_reason: str | None = None
        try:
            await client.start()
            started = True
            me = await client.get_me()
            account_id = me.id

            if bans.is_account_disabled(account_id):
                disable_reason = "already_disabled"
            else:
                pacer = _PACERS.setdefault((user_id, account_id), Pacer())
                while state.get("status") == "RUNNING":
                    await _maybe_enforce(user_id, account_id, client)
                    targets = await _collect_targets(client, account_id, state)
                    if state.get("status") != "RUNNING":
                        break
                    if not targets:
                        await asyncio.sleep(10)   # nothing to send right now
                        continue
                    message = state["message"]
                    _, disable_reason = await _send_targets(
                        client, targets, message, state, account_id, pacer
                    )
                    if disable_reason:
                        break
                    if state.get("status") == "RUNNING":
                        await asyncio.sleep(_ROUND_GAP)

        except asyncio.CancelledError:
            if started:
                try:
                    await client.stop()
                except Exception:
                    pass
            raise
        except Exception as e:
            dec = classify(e)
            if dec.action == Action.DISABLE_ACCT:
                disable_reason = dec.reason
            elif dec.action == Action.BACKOFF:
                logger.info(f"[account] setup FloodWait {dec.wait_seconds}s acc={account_id or index+1}")
                await asyncio.sleep(dec.wait_seconds + 1)
            else:
                logger.warning(f"[account] acc {index+1} (id={account_id}) error: {e}")
        finally:
            if started:
                try:
                    await client.stop()
                except Exception as e:
                    logger.debug(f"[account] stop() error acc={account_id}: {e}")

        if disable_reason:
            await _finalize_disable(user_id, index, session, account_id, disable_reason, state)
            return

        # Fell out of the inner loop (connection error / setup flood) — brief
        # backoff, then the outer while reconnects (unless stopped meanwhile).
        st = USER_STATES.get(user_id)
        if not st or st.get("status") != "RUNNING":
            return
        await asyncio.sleep(5)


async def dedicated_user_worker(user_id: int):
    """Supervisor for manual RUN: one independent perpetual worker per account.

    Spawns/restarts an ``_account_perpetual`` task per session, so accounts run
    fully decoupled — a FloodWait on one never stalls the others. Cancels all
    child tasks when the engine is stopped.
    """
    tasks: dict[str, asyncio.Task] = {}
    respawn_after: dict[str, float] = {}   # crash backoff per session

    def _cancel_all():
        for t in tasks.values():
            if not t.done():
                t.cancel()

    try:
        while True:
            state = USER_STATES.get(user_id)
            if not state:
                break
            status = state.get("status")
            if status == "PAUSED":
                _cancel_all()
                tasks.clear()
                respawn_after.clear()
                await asyncio.sleep(1)
                continue
            if status != "RUNNING":
                break
            if not state["sessions"]:
                await send_user_update(user_id, "⚠️ **Engine Stopped:** No active accounts. Please add an account.")
                state["status"] = "STOPPED"
                break

            # Reap finished children; back off ONLY the ones that crashed.
            for session, t in list(tasks.items()):
                if t.done():
                    if not t.cancelled():
                        exc = t.exception()
                        if exc:
                            logger.warning(f"[Worker] account worker crashed (backoff 10s): {exc}")
                            respawn_after[session] = time.monotonic() + 10
                    tasks.pop(session, None)

            # Spawn a worker for any session without a live one.
            now = time.monotonic()
            for i, session in enumerate(list(state["sessions"])):
                if session in tasks:
                    continue
                if now < respawn_after.get(session, 0.0):
                    continue  # crashed recently — wait out the backoff
                respawn_after.pop(session, None)
                tasks[session] = asyncio.create_task(_account_perpetual(user_id, i, session))

            # Drop workers for sessions no longer present.
            for session in list(tasks):
                if session not in state["sessions"]:
                    tsk = tasks.pop(session)
                    if not tsk.done():
                        tsk.cancel()
                    respawn_after.pop(session, None)

            await asyncio.sleep(3)
    except asyncio.CancelledError:
        logger.info(f"[Worker] Supervisor cancelled for user {user_id}")
    except Exception as e:
        logger.error(f"[Worker] Supervisor error for user {user_id}: {e}", exc_info=True)
    finally:
        _cancel_all()
        RUNNING_TASKS.pop(user_id, None)
        logger.info(f"[Worker] Supervisor ended for user {user_id}")


# ==================== ONE-SHOT PASS (scheduler) ====================
async def _account_broadcast(user_id: int, index: int, session: str, state: dict, message: str) -> str:
    """One broadcast pass for a single account. Returns 'ok'|'disabled'|'idle'."""
    if bans.is_session_disabled(session):
        _remove_session(state, session)
        return "disabled"

    client = _make_client(user_id, index, session)
    started = False
    account_id = None
    disable_reason: str | None = None
    sent_any = False
    try:
        await client.start()
        started = True
        me = await client.get_me()
        account_id = me.id

        if bans.is_account_disabled(account_id):
            disable_reason = "already_disabled"

        if not disable_reason:
            await _maybe_enforce(user_id, account_id, client)
            targets = await _collect_targets(client, account_id, state)
            if not targets:
                return "idle"
            pacer = _PACERS.setdefault((user_id, account_id), Pacer())
            sent_any, disable_reason = await _send_targets(
                client, targets, message, state, account_id, pacer
            )

    except asyncio.CancelledError:
        raise
    except Exception as e:
        dec = classify(e)
        if dec.action == Action.DISABLE_ACCT:
            disable_reason = dec.reason
        elif dec.action == Action.BACKOFF:
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
        await _finalize_disable(user_id, index, session, account_id, disable_reason, state)
        return "disabled"

    return "ok" if sent_any else "idle"


async def broadcast_once(user_id: int, state: dict, message_override: str | None = None) -> bool:
    """Run every account's broadcast pass concurrently (one pass each)."""
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
