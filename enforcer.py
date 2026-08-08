"""Vexora Ads branding enforcer.

Per-account tasks:
* Ensure ``first_name`` ends with ``VEXORA_NAME_SUFFIX``.
* Ensure bio matches ``VEXORA_BIO``.
* Ensure account is a member of every link in ``VEXORA_CHANNELS`` (rejoin
  automatically if the user leaves).

The global loop polls every 90s and skips accounts that are actively
broadcasting to avoid parallel-client conflicts on the same session.
"""
import asyncio

from pyrogram import Client
from pyrogram.errors import FloodWait

from config import (
    API_ID, API_HASH, logger,
    VEXORA_BIO, VEXORA_NAME_SUFFIX, VEXORA_CHANNELS,
)
from state import USER_STATES


def _apply_suffix(name: str | None) -> str:
    base = (name or "").strip()
    suffix_trim = VEXORA_NAME_SUFFIX.strip()
    if suffix_trim in base:
        return base
    max_base = 64 - len(VEXORA_NAME_SUFFIX)
    trimmed = base[:max_base].rstrip()
    return (trimmed + VEXORA_NAME_SUFFIX).strip()


def _make_client(user_id: int, idx: int, session: str) -> Client:
    return Client(
        f"enforce_{user_id}_{idx}",
        session_string=session,
        api_id=API_ID, api_hash=API_HASH,
        in_memory=True, no_updates=True,
        device_model="PC 64bit",
        system_version="Windows 11",
        app_version="4.16.3",
    )


async def enforce_account(user_app: Client, do_join: bool = True):
    """Run one enforcement pass on an already-started Client."""
    # Name
    try:
        me = await user_app.get_me()
        desired_name = _apply_suffix(me.first_name)
        if me.first_name != desired_name:
            await user_app.update_profile(first_name=desired_name)
            logger.info(f"[enforcer] name updated for {me.id}")
    except FloodWait as e:
        await asyncio.sleep(e.value + 1)
    except Exception as e:
        logger.debug(f"[enforcer] name step failed: {e}")

    # Bio
    try:
        chat = await user_app.get_chat("me")
        current_bio = getattr(chat, "bio", None) or ""
        if current_bio != VEXORA_BIO:
            await user_app.update_profile(bio=VEXORA_BIO)
            logger.info(f"[enforcer] bio updated for {chat.id}")
    except FloodWait as e:
        await asyncio.sleep(e.value + 1)
    except Exception as e:
        logger.debug(f"[enforcer] bio step failed: {e}")

    # Channels
    if do_join:
        for link in VEXORA_CHANNELS:
            try:
                await user_app.join_chat(link)
                logger.info(f"[enforcer] joined {link}")
            except FloodWait as e:
                await asyncio.sleep(e.value + 1)
            except Exception as e:
                # Most common: USER_ALREADY_PARTICIPANT — expected/benign.
                logger.debug(f"[enforcer] join {link}: {e}")


async def enforce_session_once(user_id: int, idx: int, session: str, do_join: bool = True):
    """Connect once, enforce, disconnect."""
    c = _make_client(user_id, idx, session)
    started = False
    try:
        await c.start()
        started = True
        await enforce_account(c, do_join=do_join)
    except Exception as e:
        logger.warning(f"[enforcer] session {user_id}/{idx} failed: {e}")
    finally:
        if started:
            try:
                await c.stop()
            except Exception:
                pass


async def logout_session(user_id: int, session: str):
    """Best-effort remote logout, invalidating the session server-side."""
    c = _make_client(user_id, 0, session)
    try:
        await c.start()
        try:
            await c.log_out()  # Pyrogram: invalidates + disconnects.
            logger.info(f"[enforcer] logout ok for user {user_id}")
        except Exception as e:
            logger.warning(f"[enforcer] log_out failed for user {user_id}: {e}")
            try:
                await c.stop()
            except Exception:
                pass
    except Exception as e:
        logger.warning(f"[enforcer] connect-for-logout failed for user {user_id}: {e}")


async def enforcer_loop():
    """Global loop — visits every session ~ every 90s."""
    logger.info("[enforcer] Loop started.")
    while True:
        try:
            for uid, state in list(USER_STATES.items()):
                if state.get("status") == "RUNNING":
                    continue  # avoid dual clients on the same session
                for idx, session in enumerate(list(state.get("sessions", []))):
                    try:
                        await enforce_session_once(uid, idx, session, do_join=True)
                    except Exception as e:
                        logger.warning(f"[enforcer] pass err uid={uid} idx={idx}: {e}")
                    await asyncio.sleep(2)
        except Exception as e:
            logger.error(f"[enforcer] loop error: {e}", exc_info=True)
        await asyncio.sleep(90)
