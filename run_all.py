"""Single-process launcher — runs the main Vexora Ads bot and (optionally)
the Vexora Admin bot in ONE event loop.

IMPORTANT: the main bot ALWAYS starts. The admin bot is best-effort — if its
env vars are missing/wrong it is skipped with a clear log line and the main
bot keeps running. This is the recommended entry point on Railway.

Railway → Settings → Deploy → Custom Start Command:
    python run_all.py

Env vars (all on the SAME service):
    Main : BOT_TOKEN, API_ID, API_HASH, NEON_DATABASE_URL
    Admin: ADMIN_BOT_TOKEN, ADMIN_IDS   (+ shares API_ID/API_HASH/NEON_DATABASE_URL)
"""
import asyncio

from config import logger
from client import app
import handlers  # noqa: F401 — registers main-bot handlers
from state import USER_STATES, RUNNING_TASKS
from scheduler import scheduler_loop
from enforcer import enforcer_loop
from sender import dedicated_user_worker
import db


async def _start_admin_bot():
    """Best-effort admin-bot boot. NEVER fatal to the main bot."""
    try:
        from adminclient import admin_app
        import adminhandlers
        import adminstore
    except Exception as e:
        logger.warning(f"[run_all] Admin bot skipped (config not ready): {e}")
        logger.warning("[run_all] Set ADMIN_BOT_TOKEN + ADMIN_IDS to enable the admin bot. Main bot is running.")
        return
    try:
        await adminstore.init_pool()
        await adminhandlers.load_dynamic_admins()
        await admin_app.start()
        me = await admin_app.get_me()
        logger.info(f"[run_all] Admin bot online as @{me.username}")
    except Exception as e:
        logger.error(f"[run_all] Admin bot failed to start: {e}")
        logger.error("[run_all] Verify ADMIN_BOT_TOKEN is a valid @BotFather token. Main bot keeps running.")


async def start_all():
    # ---- main bot (mandatory) ----
    logger.info("[run_all] Booting main bot...")
    await db.init_db()
    loaded = await db.load_all()
    if loaded:
        USER_STATES.update(loaded)
    await app.start()
    me_main = await app.get_me()
    logger.info(f"[run_all] Main bot online as @{me_main.username}")

    for uid, state in list(USER_STATES.items()):
        if state.get("status") in ("RUNNING", "PAUSED") and state.get("sessions"):
            state["status"] = "RUNNING"
            RUNNING_TASKS[uid] = asyncio.create_task(dedicated_user_worker(uid))
            logger.info(f"[run_all] resumed broadcast worker for user {uid}")
        elif state.get("status") in ("RUNNING", "PAUSED"):
            state["status"] = "STOPPED"

    asyncio.create_task(scheduler_loop())
    asyncio.create_task(enforcer_loop())

    # ---- admin bot (optional) ----
    await _start_admin_bot()


async def _bootstrap():
    await start_all()
    logger.info("[run_all] Ready.")
    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        app.run(_bootstrap())
    except KeyboardInterrupt:
        logger.info("[run_all] Stopped.")
