"""MultiControlBot — entrypoint.

Modules:
  config      env, logging, PREMIUM_EMOJI map
  premium     custom-emoji MessageEntity helpers
  client      shared Pyrogram Client singleton
  state       USER_STATES, RUNNING_TASKS, init & cleanup
  ui          keyboards, status text, OTP + schedules UI, safe_edit_text
  sender      broadcast_once, dedicated_user_worker, run_scheduled_broadcast
  scheduler   daily / interval schedule engine
  handlers    all /start, keyboard, callback handlers (registered on import)
"""
import asyncio

from config import logger
from client import app
import handlers  # noqa: F401  — registers @app.on_message / @app.on_callback_query
from scheduler import scheduler_loop


async def _startup(_client):
    logger.info("🚀 Bot Engine Starting...")
    asyncio.create_task(scheduler_loop())


if __name__ == "__main__":
    # Start scheduler once the Client loop is ready.
    async def _main():
        await app.start()
        await _startup(app)
        logger.info("✅ Bot online.")
        await asyncio.Event().wait()

    try:
        app.run(_main())
    except KeyboardInterrupt:
        logger.info("Bot stopped.")
