"""Keyboards, status text, OTP + schedule UI, safe edit + send helpers.

Design notes:
* Reply-keyboard buttons carry a text label WITHOUT emojis. The premium
  emoji icon is attached via ``KeyboardButtonStyle.icon``, so the button
  shows one clean premium emoji next to plain-text label (no doubles).
* All outgoing messages go through :func:`send_premium` / :func:`reply_premium`
  / :func:`edit_premium` which parse **markdown** and merge premium custom
  emoji entities in one shot.
"""
import time
import asyncio

from pyrogram.types import (
    ReplyKeyboardMarkup, KeyboardButton, KeyboardButtonStyle,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from pyrogram.errors import FloodWait, MessageNotModified, RPCError

from config import PREMIUM_EMOJI, logger
from premium import build_message
from client import app
from state import USER_STATES


# ==================== SEND HELPERS ====================
async def reply_premium(message, text: str, reply_markup=None):
    body, ents = await build_message(text)
    return await message.reply_text(body, reply_markup=reply_markup, entities=ents)


async def send_premium(chat_id: int, text: str, reply_markup=None):
    body, ents = await build_message(text)
    return await app.send_message(chat_id, body, reply_markup=reply_markup, entities=ents)


async def edit_premium(message, text: str, reply_markup=None):
    body, ents = await build_message(text)
    return await message.edit_text(body, reply_markup=reply_markup, entities=ents)


# ==================== SAFE EDIT ====================
_last_edits: dict = {}


async def safe_edit_text(message, text: str, reply_markup=None, min_interval: float = 2.5):
    if not message:
        return
    msg_key = (message.chat.id, message.id)
    now = time.time()
    if msg_key in _last_edits and (now - _last_edits[msg_key]) < min_interval:
        return
    if len(_last_edits) > 500:
        cutoff = now - 300
        stale = [k for k, v in _last_edits.items() if v < cutoff]
        for k in stale:
            del _last_edits[k]
    try:
        await edit_premium(message, text, reply_markup=reply_markup)
        _last_edits[msg_key] = time.time()
    except FloodWait as e:
        if e.value <= 10:
            await asyncio.sleep(e.value)
            try:
                await edit_premium(message, text, reply_markup=reply_markup)
                _last_edits[msg_key] = time.time()
            except Exception as inner_e:
                logger.debug(f"[safe_edit_text] retry after FloodWait failed: {inner_e}")
    except MessageNotModified:
        pass
    except RPCError as e:
        logger.debug(f"[safe_edit_text] RPCError: {e}")
    except Exception as e:
        logger.warning(f"[safe_edit_text] Unexpected error: {e}")


async def send_user_update(user_id: int, text: str):
    try:
        await send_premium(user_id, text)
    except Exception as e:
        logger.warning(f"[send_user_update] Failed for user {user_id}: {e}")


# ==================== REPLY KEYBOARDS ====================
def styled_button(text: str, icon_emoji: str, colour: str = "blue") -> KeyboardButton:
    """Reply-keyboard button with a native Telegram Premium icon.

    ``text`` is a plain label (no leading emoji), because the icon renders
    a premium emoji next to it. ``icon_emoji`` is a key from PREMIUM_EMOJI.
    """
    style = KeyboardButtonStyle(
        bg_primary=colour == "blue",
        bg_danger=colour == "red",
        bg_success=colour == "green",
        icon=int(PREMIUM_EMOJI[icon_emoji]),
    )
    return KeyboardButton(text, style=style)


# Canonical button labels — no emojis, matched exactly in handlers.
BTN_RUN            = "RUN"
BTN_PAUSE          = "PAUSE"
BTN_STOP           = "STOP"
BTN_SET_DELAY      = "Set Delay"
BTN_SET_MESSAGE    = "Set Message"
BTN_MODE_NORMAL    = "Mode: NORMAL"
BTN_MODE_ADVANCED  = "Mode: ADVANCED"
BTN_REFRESH        = "Refresh"
BTN_ADD_ACC        = "Add Account"
BTN_REMOVE_ACC     = "Remove Account"
BTN_SCHEDULES      = "Schedules"
BTN_CANCEL         = "Cancel"


def get_premium_keyboard(sending_mode: str = "NORMAL") -> ReplyKeyboardMarkup:
    if sending_mode == "ADVANCED":
        mode_label = BTN_MODE_ADVANCED
        mode_icon = "🚀"
    else:
        mode_label = BTN_MODE_NORMAL
        mode_icon = "🐢"
    return ReplyKeyboardMarkup([
        [styled_button(BTN_RUN,      "▶️", "green"),
         styled_button(BTN_PAUSE,    "⏸️", "blue"),
         styled_button(BTN_STOP,     "⏹️", "red")],
        [styled_button(BTN_SET_DELAY,   "⏱️", "blue"),
         styled_button(BTN_SET_MESSAGE, "📝", "blue")],
        [styled_button(mode_label,   mode_icon, "blue"),
         styled_button(BTN_REFRESH,  "🔄", "blue")],
        [styled_button(BTN_ADD_ACC,    "➕", "green"),
         styled_button(BTN_REMOVE_ACC, "➖", "red")],
        [styled_button(BTN_SCHEDULES,  "🗓", "blue")],
    ], resize_keyboard=True, one_time_keyboard=False)


def cancel_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[styled_button(BTN_CANCEL, "🔙", "blue")]],
        resize_keyboard=True,
    )


def remove_account_keyboard(count: int) -> ReplyKeyboardMarkup:
    rows = [
        [styled_button(f"Remove Profile {i+1}", "❌", "red")]
        for i in range(count)
    ]
    rows.append([styled_button(BTN_CANCEL, "🔙", "blue")])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


# ==================== TEXTS ====================
WELCOME_TEXT = (
    "⚡ **Control Panel**\n"
    "\n"
    "Accounts, promo, delay, mode, schedules — everything sits in the tray below.\n"
    "Tap what you need."
)


def get_status_text(user_id: int) -> str:
    st = USER_STATES[user_id]
    schedules = st.get("schedules", [])
    sch_active = sum(1 for s in schedules if s.get("enabled"))
    return (
        "⚡ **Control Panel**\n"
        "━━━━━━━━━━━━━━\n"
        f"Status      ·  `{st['status']}`\n"
        f"Mode        ·  `{st.get('sending_mode', 'NORMAL')}`\n"
        f"Delay       ·  `{st['delay']}s`\n"
        f"Accounts    ·  `{len(st['sessions'])}`\n"
        f"Schedules   ·  `{sch_active}/{len(schedules)} on`\n"
        "\n"
        f"Sent  `{st['sent_count']}`     Failed  `{st['failed_count']}`\n"
        "━━━━━━━━━━━━━━\n"
        "Message\n"
        f"_{st['message']}_"
    )


# ==================== OTP KEYPAD ====================
def get_otp_inline_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("1", callback_data="otp_key:1"),
         InlineKeyboardButton("2", callback_data="otp_key:2"),
         InlineKeyboardButton("3", callback_data="otp_key:3")],
        [InlineKeyboardButton("4", callback_data="otp_key:4"),
         InlineKeyboardButton("5", callback_data="otp_key:5"),
         InlineKeyboardButton("6", callback_data="otp_key:6")],
        [InlineKeyboardButton("7", callback_data="otp_key:7"),
         InlineKeyboardButton("8", callback_data="otp_key:8"),
         InlineKeyboardButton("9", callback_data="otp_key:9")],
        [InlineKeyboardButton("Del",    callback_data="otp_key:del"),
         InlineKeyboardButton("0",      callback_data="otp_key:0"),
         InlineKeyboardButton("Submit", callback_data="otp_key:submit")],
        [InlineKeyboardButton("Cancel", callback_data="otp_key:cancel")],
    ])


def format_otp_display(current_otp: str, total_slots: int = 5) -> str:
    chars = []
    for i in range(total_slots):
        chars.append(f"**{current_otp[i]}**" if i < len(current_otp) else "**_**")
    return "  ".join(chars)


# ==================== SCHEDULES UI ====================
def format_schedule_label(sch: dict) -> str:
    dot = "on" if sch.get("enabled") else "off"
    if sch["kind"] == "daily":
        return f"[{dot}] Daily · {sch['time']} UTC"
    return f"[{dot}] Every {sch['interval_minutes']} min"


def get_schedules_text(user_id: int) -> str:
    st = USER_STATES[user_id]
    schedules = st.get("schedules", [])
    header = "🗓 **Schedules**\n━━━━━━━━━━━━━━\n"
    if not schedules:
        return header + (
            "No schedules yet.\n\n"
            "• Daily — fires once a day at fixed UTC time.\n"
            "• Interval — repeats every N minutes.\n\n"
            "Add one below."
        )
    return header + f"{len(schedules)} configured. Toggle or remove below."


def get_schedules_inline(user_id: int) -> InlineKeyboardMarkup:
    st = USER_STATES[user_id]
    buttons = []
    for sch in st.get("schedules", []):
        buttons.append([
            InlineKeyboardButton(format_schedule_label(sch), callback_data=f"sch:noop:{sch['id']}"),
            InlineKeyboardButton("Pause" if sch.get("enabled") else "Run", callback_data=f"sch:toggle:{sch['id']}"),
            InlineKeyboardButton("Del", callback_data=f"sch:del:{sch['id']}"),
        ])
    buttons.append([
        InlineKeyboardButton("Add Daily",    callback_data="sch:add:daily"),
        InlineKeyboardButton("Add Interval", callback_data="sch:add:interval"),
    ])
    buttons.append([InlineKeyboardButton("Close", callback_data="sch:close")])
    return InlineKeyboardMarkup(buttons)
