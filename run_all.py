"""Single-process launcher — runs BOTH the main Vexora Ads bot and the
Vexora Admin bot in one event loop.

Use this on Railway when you want ONE service to run everything (recommended):
    start command:  python run_all.py

Env vars required (all in the SAME service):
    BOT_TOKEN, ADMIN_BOT_TOKEN, API_ID, API_HASH, ADMIN_IDS, NEON_DATABASE_URL
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

from adminclient import admin_app
import adminhandlers  # noqa: F401 — registers admin-bot handlers
import adminstore


async def start_all():
    # ---- main bot ----
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

    # ---- admin bot ----
    logger.info("[run_all] Booting admin bot...")
    try:
        await adminstore.init_pool()
        await adminhandlers.load_dynamic_admins()
        await admin_app.start()
        me_admin = await admin_app.get_me()
        logger.info(f"[run_all] Admin bot online as @{me_admin.username}")
    except Exception as e:
        logger.error(f"[run_all] ADMIN BOT FAILED TO START: {e}", exc_info=True)
        logger.error("[run_all] Main bot keeps running. Check ADMIN_BOT_TOKEN / API creds.")


async def _bootstrap():
    await start_all()
    logger.info("[run_all] Both bots online.")
    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        app.run(_bootstrap())
    except KeyboardInterrupt:
        logger.info("[run_all] Stopped.")
