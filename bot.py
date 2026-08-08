"""Vexora Ads Bot — entrypoint.

Boots the Pyrogram Client, wires handlers, connects to Neon Postgres
(optional), loads persisted user state, then launches the scheduler and
brand-enforcer background loops.
"""
import asyncio

from config import logger
from client import app
import handlers  # noqa: F401  — registers @app.on_message / @app.on_callback_query
from state import USER_STATES, RUNNING_TASKS
from scheduler import scheduler_loop
from enforcer import enforcer_loop
from sender import dedicated_user_worker
import db


async def _bootstrap():
    logger.info("Vexora Ads Bot starting...")
    await db.init_db()
    loaded = await db.load_all()
    if loaded:
        USER_STATES.update(loaded)
    await app.start()

    # Reconcile persisted status with reality: a user loaded as RUNNING/PAUSED
    # has no live worker task after a fresh process start. Left alone this
    # permanently blocks enforcer_loop from ever enforcing that account
    # (it skips anything with status == "RUNNING"), so VEXORA_CHAT_IDS would
    # never get populated for it. Either resume the worker or drop the
    # stale status back to STOPPED so enforcement can proceed normally.
    for uid, state in list(USER_STATES.items()):
        if state.get("status") in ("RUNNING", "PAUSED") and state.get("sessions"):
            state["status"] = "RUNNING"
            RUNNING_TASKS[uid] = asyncio.create_task(dedicated_user_worker(uid))
            logger.info(f"[bootstrap] resumed broadcast worker for user {uid}")
        elif state.get("status") in ("RUNNING", "PAUSED"):
            state["status"] = "STOPPED"

    asyncio.create_task(scheduler_loop())
    asyncio.create_task(enforcer_loop())
    logger.info("Vexora Ads Bot online.")
    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        app.run(_bootstrap())
    except KeyboardInterrupt:
        logger.info("Bot stopped.")
