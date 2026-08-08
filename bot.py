"""Vexora Ads Bot — entrypoint.

Boots the Pyrogram Client, wires handlers, connects to Neon Postgres
(optional), loads persisted user state, then launches the scheduler and
brand-enforcer background loops.
"""
import asyncio

from config import logger
from client import app
import handlers  # noqa: F401  — registers @app.on_message / @app.on_callback_query
from state import USER_STATES
from scheduler import scheduler_loop
from enforcer import enforcer_loop
import db


async def _bootstrap():
    logger.info("Vexora Ads Bot starting...")
    await db.init_db()
    loaded = await db.load_all()
    if loaded:
        USER_STATES.update(loaded)
    await app.start()
    asyncio.create_task(scheduler_loop())
    asyncio.create_task(enforcer_loop())
    logger.info("Vexora Ads Bot online.")
    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        app.run(_bootstrap())
    except KeyboardInterrupt:
        logger.info("Bot stopped.")
