"""Keyboards, status text, safe edit, OTP helpers — all user-facing UI."""
import time
import asyncio

from pyrogram.types import (
    ReplyKeyboardMarkup, KeyboardButton, KeyboardButtonStyle,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from pyrogram.errors import FloodWait, MessageNotModified, RPCError

from config import PREMIUM_EMOJI, logger
from premium import premium_kwargs
from client import app
from state import USER_STATES

_last_edits: dict = {}


# ==================== SAFE MESSAGE EDIT ====================
async def safe_edit_text(message, text, reply_markup=None, parse_mode=None, min_interval: float = 2.5):
    if not message:
        return
    msg_key = (message.chat.id, message.id)
    now = time.time()
    if msg_key in _last_edits and (now - _last_edits[msg_key]) < min_interval:
        return
    # Prune stale entries to avoid unbounded growth
    if len(_last_edits) > 500:
        cutoff = now - 300
        stale = [k for k, v in _last_edits.items() if v < cutoff]
        for k in stale:
            del _last_edits[k]
    try:
        await message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode, **premium_kwargs(text))
        _last_edits[msg_key] = time.time()
    except FloodWait as e:
        if e.value <= 10:
            await asyncio.sleep(e.value)
            try:
                await message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode, **premium_kwargs(text))
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
        await app.send_message(user_id, text, **premium_kwargs(text))
    except Exception as e:
        logger.warning(f"[send_user_update] Failed for user {user_id}: {e}")


# ==================== REPLY KEYBOARDS ====================
def styled_button(text: str, custom_id: str, colour: str = "blue") -> KeyboardButton:
    style = KeyboardButtonStyle(
        bg_primary=colour == "blue",
        bg_danger=colour == "red",
        bg_success=colour == "green",
        icon=int(custom_id),
    )
    return KeyboardButton(text, style=style)


def get_premium_keyboard(sending_mode: str = "NORMAL") -> ReplyKeyboardMarkup:
    mode_text = "🚀 Mode: ADVANCED" if sending_mode == "ADVANCED" else "🐢 Mode: NORMAL"
    return ReplyKeyboardMarkup([
        [styled_button("▶️ RUN", PREMIUM_EMOJI["▶️"], "green"),
         styled_button("⏸️ PAUSE", PREMIUM_EMOJI["⏸️"], "blue"),
         styled_button("⏹️ STOP", PREMIUM_EMOJI["⏹️"], "red")],
        [styled_button("⏱️ Set Delay", PREMIUM_EMOJI["⏱️"], "blue"),
         styled_button("📝 Set Message", PREMIUM_EMOJI["📝"], "blue")],
        [styled_button(mode_text, PREMIUM_EMOJI["🚀" if sending_mode == "ADVANCED" else "🐢"], "blue"),
         styled_button("🔄 Refresh Status", PREMIUM_EMOJI["🔄"], "blue")],
        [styled_button("➕ Add Account", PREMIUM_EMOJI["➕"], "green"),
         styled_button("➖ Remove Account", PREMIUM_EMOJI["➖"], "red")],
        [styled_button("🗓 Schedules", PREMIUM_EMOJI["🗓"], "blue")],
    ], resize_keyboard=True, one_time_keyboard=False)


def cancel_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[styled_button("🔙 Cancel Operational Mode", PREMIUM_EMOJI["🔙"], "blue")]],
        resize_keyboard=True,
    )


# ==================== STATUS TEXT ====================
def get_status_text(user_id: int) -> str:
    state = USER_STATES[user_id]
    schedules = state.get("schedules", [])
    sch_active = sum(1 for s in schedules if s.get("enabled"))
    return (
        f"⚡ **CONTROL ROOM** ⚡\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📊 **Status**  ·  `{state['status']}`\n"
        f"⚙️ **Mode**  ·  `{state.get('sending_mode', 'NORMAL')}`\n"
        f"⏱️ **Delay**  ·  `{state['delay']} sec`\n"
        f"👥 **Accounts**  ·  `{len(state['sessions'])}`\n"
        f"🗓 **Schedules**  ·  `{sch_active}/{len(schedules)} active`\n\n"
        f"✅ **Sent**  `{state['sent_count']}`     ❌ **Failed**  `{state['failed_count']}`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"💬 **Current promo message**\n_{state['message']}_"
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
        [InlineKeyboardButton("⌫ Delete", callback_data="otp_key:del"),
         InlineKeyboardButton("0",         callback_data="otp_key:0"),
         InlineKeyboardButton("✅ Submit",  callback_data="otp_key:submit")],
        [InlineKeyboardButton("❌ Cancel Login", callback_data="otp_key:cancel")],
    ])


def format_otp_display(current_otp: str, total_slots: int = 5) -> str:
    chars = []
    for i in range(total_slots):
        chars.append(f"**{current_otp[i]}**" if i < len(current_otp) else "**_**")
    return "  ".join(chars)


# ==================== SCHEDULES UI ====================
def format_schedule_label(sch: dict) -> str:
    dot = "🟢" if sch.get("enabled") else "⏸️"
    if sch["kind"] == "daily":
        base = f"{dot} Daily · {sch['time']} UTC"
    else:
        base = f"{dot} Every {sch['interval_minutes']} min"
    if sch.get("message"):
        base += "  ✎"
    return base


def get_schedules_text(user_id: int) -> str:
    state = USER_STATES[user_id]
    schedules = state.get("schedules", [])
    header = (
        "🗓 **Broadcast Schedules**\n"
        "━━━━━━━━━━━━━━━━━━\n"
    )
    if not schedules:
        return header + (
            "_No schedules yet._\n\n"
            "• **Daily** — fires once every 24h at a fixed UTC time.\n"
            "• **Interval** — repeats every N minutes.\n\n"
            "Use the buttons below to add one."
        )
    return header + (
        f"_{len(schedules)} configured. Tap ⏸️/▶️ to toggle or 🗑 to remove._"
    )


def get_schedules_inline(user_id: int) -> InlineKeyboardMarkup:
    state = USER_STATES[user_id]
    buttons = []
    for sch in state.get("schedules", []):
        buttons.append([
            InlineKeyboardButton(format_schedule_label(sch), callback_data=f"sch:noop:{sch['id']}"),
            InlineKeyboardButton("⏸️" if sch.get("enabled") else "▶️", callback_data=f"sch:toggle:{sch['id']}"),
            InlineKeyboardButton("🗑", callback_data=f"sch:del:{sch['id']}"),
        ])
    buttons.append([
        InlineKeyboardButton("➕ Add Daily", callback_data="sch:add:daily"),
        InlineKeyboardButton("➕ Add Interval", callback_data="sch:add:interval"),
    ])
    buttons.append([InlineKeyboardButton("🔙 Close", callback_data="sch:close")])
    return InlineKeyboardMarkup(buttons)
