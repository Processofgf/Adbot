"""Pyrogram Client singleton for the Vexora Admin Bot."""
from pyrogram import Client
from adminconfig import API_ID, API_HASH, ADMIN_BOT_TOKEN

admin_app = Client(
    "VexoraAdminBot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=ADMIN_BOT_TOKEN,
)
