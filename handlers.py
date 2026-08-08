"""Telegram command / message / callback handlers."""
import asyncio

from pyrogram import filters, Client
from pyrogram.errors import SessionPasswordNeeded

from client import app
from config import API_ID, API_HASH, logger
from state import (
    USER_STATES, RUNNING_TASKS, initialize_user_state, cleanup_user_login,
)
from ui import (
    get_premium_keyboard, cancel_keyboard, remove_account_keyboard,
    get_status_text, WELCOME_TEXT,
    get_otp_inline_keyboard, format_otp_display,
    get_schedules_inline, get_schedules_text,
    reply_premium, send_premium, safe_edit_text,
    BTN_RUN, BTN_PAUSE, BTN_STOP, BTN_SET_DELAY, BTN_SET_MESSAGE,
    BTN_MODE_NORMAL, BTN_MODE_ADVANCED, BTN_REFRESH,
    BTN_ADD_ACC, BTN_REMOVE_ACC, BTN_SCHEDULES, BTN_CANCEL,
)
from sender import dedicated_user_worker
from scheduler import (
    add_daily_schedule, add_interval_schedule, remove_schedule, toggle_schedule,
    parse_hhmm, parse_interval_minutes,
)


# ==================== /start & /panel ====================
@app.on_message(filters.command(["start", "panel"]))
async def show_panel(client, message):
    user_id = message.from_user.id
    state = initialize_user_state(user_id)
    state["waiting_for"] = None
    await cleanup_user_login(user_id, state)

    if message.command and message.command[0].lower() == "start":
        await reply_premium(message, WELCOME_TEXT, reply_markup=get_premium_keyboard(state["sending_mode"]))
    else:
        await reply_premium(message, get_status_text(user_id), reply_markup=get_premium_keyboard(state["sending_mode"]))


# ==================== OTP INLINE KEYPAD ====================
@app.on_callback_query(filters.regex(r"^otp_key:"))
async def otp_keypad_handler(client, callback_query):
    user_id = callback_query.from_user.id
    state = initialize_user_state(user_id)
    action = callback_query.data.split(":")[1]

    if state["waiting_for"] != "otp" or "client" not in state["login_data"]:
        await callback_query.answer("Session expired.", show_alert=True)
        return

    login_data = state["login_data"]
    current_otp = login_data.get("current_otp", "")

    if action == "cancel":
        await callback_query.answer("Cancelled")
        state["waiting_for"] = None
        await cleanup_user_login(user_id, state)
        await safe_edit_text(callback_query.message, "**Login cancelled.**", min_interval=0)
        await send_premium(user_id, "Back to panel.", reply_markup=get_premium_keyboard(state["sending_mode"]))
        return

    if action == "del":
        if current_otp:
            current_otp = current_otp[:-1]
            login_data["current_otp"] = current_otp
            otp_display = format_otp_display(current_otp)
            await safe_edit_text(
                callback_query.message,
                f"**Enter OTP for {login_data['phone']}**\n\n`[ {otp_display} ]`",
                reply_markup=get_otp_inline_keyboard(),
                min_interval=0.3,
            )
        await callback_query.answer()
        return

    if action == "submit":
        if len(current_otp) < 5:
            await callback_query.answer("Enter all 5 digits.", show_alert=True)
            return
        await process_otp_login(client, callback_query.message, user_id, state, current_otp)
        await callback_query.answer()
        return

    # digit
    if len(current_otp) < 5:
        current_otp += action
        login_data["current_otp"] = current_otp
        otp_display = format_otp_display(current_otp)

        if len(current_otp) == 5:
            await safe_edit_text(
                callback_query.message,
                f"**Verifying...**\n\n`[ {otp_display} ]`",
                reply_markup=None, min_interval=0,
            )
            await callback_query.answer("Verifying...")
            await process_otp_login(client, callback_query.message, user_id, state, current_otp)
            return
        await safe_edit_text(
            callback_query.message,
            f"**Enter OTP for {login_data['phone']}**\n\n`[ {otp_display} ]`",
            reply_markup=get_otp_inline_keyboard(),
            min_interval=0.2,
        )
        await callback_query.answer()


async def process_otp_login(client, message, user_id: int, state: dict, otp_code: str):
    login_data = state["login_data"]
    temp_client = login_data["client"]
    try:
        await temp_client.sign_in(login_data["phone"], login_data["phone_code_hash"], otp_code)
        string_session = await temp_client.export_session_string()
        state["sessions"].append(string_session)

        await temp_client.disconnect()
        state["waiting_for"] = None
        state["login_data"] = {}
        await safe_edit_text(message, "**Account added.**", min_interval=0)
        await send_premium(user_id, get_status_text(user_id), reply_markup=get_premium_keyboard(state["sending_mode"]))

    except SessionPasswordNeeded:
        state["waiting_for"] = "password"
        await safe_edit_text(message, "**2FA needed.** Send your cloud password.", min_interval=0)

    except Exception as e:
        logger.warning(f"[process_otp_login] OTP failed for user {user_id}: {e}")
        try:
            await temp_client.disconnect()
        except Exception:
            pass
        state["waiting_for"] = None
        state["login_data"] = {}
        await safe_edit_text(message, "**Wrong OTP.**", min_interval=0)
        await send_premium(user_id, "Back to panel.", reply_markup=get_premium_keyboard(state["sending_mode"]))


# ==================== SCHEDULES INLINE CALLBACKS ====================
@app.on_callback_query(filters.regex(r"^sch:"))
async def schedules_callback(client, callback_query):
    user_id = callback_query.from_user.id
    state = initialize_user_state(user_id)
    parts = callback_query.data.split(":")
    action = parts[1]

    if action == "close":
        await callback_query.answer("Closed")
        try:
            await callback_query.message.delete()
        except Exception:
            pass
        return

    if action == "noop":
        await callback_query.answer()
        return

    if action == "toggle":
        sch_id = parts[2]
        if toggle_schedule(state, sch_id):
            await callback_query.answer("Toggled")
        else:
            await callback_query.answer("Not found", show_alert=True)
        await safe_edit_text(
            callback_query.message,
            get_schedules_text(user_id),
            reply_markup=get_schedules_inline(user_id),
            min_interval=0,
        )
        return

    if action == "del":
        sch_id = parts[2]
        if remove_schedule(state, sch_id):
            await callback_query.answer("Removed")
        else:
            await callback_query.answer("Not found", show_alert=True)
        await safe_edit_text(
            callback_query.message,
            get_schedules_text(user_id),
            reply_markup=get_schedules_inline(user_id),
            min_interval=0,
        )
        return

    if action == "add":
        kind = parts[2]
        if kind == "daily":
            state["waiting_for"] = "sch_daily"
            await callback_query.answer()
            await send_premium(
                user_id,
                "**Add Daily Schedule**\nSend time as `HH:MM` (24h, UTC). Example: `14:30`.",
                reply_markup=cancel_keyboard(),
            )
        else:
            state["waiting_for"] = "sch_interval"
            await callback_query.answer()
            await send_premium(
                user_id,
                "**Add Interval Schedule**\nSend interval in minutes (1 to 1440). Example: `60`.",
                reply_markup=cancel_keyboard(),
            )


# ==================== TEXT / KEYBOARD HANDLERS ====================
@app.on_message(filters.text & filters.private)
async def user_text_handler(client, message):
    user_id = message.from_user.id
    text = message.text.strip()
    state = initialize_user_state(user_id)
    kb = get_premium_keyboard(state["sending_mode"])

    # ---- keyboard buttons (plain labels, no emoji) ----
    if text == BTN_REFRESH:
        state["waiting_for"] = None
        await cleanup_user_login(user_id, state)
        await reply_premium(message, get_status_text(user_id), reply_markup=kb)
        return

    if text == BTN_RUN:
        if state["status"] == "RUNNING":
            await reply_premium(message, "Engine already running.", reply_markup=kb)
            return
        state["status"] = "RUNNING"
        if user_id not in RUNNING_TASKS or RUNNING_TASKS[user_id].done():
            RUNNING_TASKS[user_id] = asyncio.create_task(dedicated_user_worker(user_id))
        await reply_premium(message, "Engine started.", reply_markup=kb)
        return

    if text == BTN_PAUSE:
        state["status"] = "PAUSED"
        await reply_premium(message, "Engine paused.", reply_markup=kb)
        return

    if text == BTN_STOP:
        state["status"] = "STOPPED"
        state["sent_count"] = 0
        state["failed_count"] = 0
        if user_id in RUNNING_TASKS and not RUNNING_TASKS[user_id].done():
            RUNNING_TASKS[user_id].cancel()
        await reply_premium(message, "Engine stopped. Counters reset.", reply_markup=kb)
        return

    if text in (BTN_MODE_NORMAL, BTN_MODE_ADVANCED):
        state["sending_mode"] = "ADVANCED" if state["sending_mode"] == "NORMAL" else "NORMAL"
        await reply_premium(message, get_status_text(user_id), reply_markup=get_premium_keyboard(state["sending_mode"]))
        return

    if text == BTN_SET_DELAY:
        state["waiting_for"] = "delay"
        await reply_premium(message, "Send delay in seconds.", reply_markup=cancel_keyboard())
        return

    if text == BTN_SET_MESSAGE:
        state["waiting_for"] = "message"
        await reply_premium(message, "Send your promo message.", reply_markup=cancel_keyboard())
        return

    if text == BTN_ADD_ACC:
        state["waiting_for"] = "phone"
        await reply_premium(
            message,
            "Send phone number with country code.\nExample: `+911234567890`",
            reply_markup=cancel_keyboard(),
        )
        return

    if text == BTN_REMOVE_ACC:
        if not state["sessions"]:
            await reply_premium(message, "No accounts to remove.", reply_markup=kb)
            return
        await reply_premium(message, "Pick an account to remove.", reply_markup=remove_account_keyboard(len(state["sessions"])))
        return

    if text == BTN_SCHEDULES:
        state["waiting_for"] = None
        await reply_premium(message, get_schedules_text(user_id), reply_markup=get_schedules_inline(user_id))
        return

    if text == BTN_CANCEL:
        state["waiting_for"] = None
        await cleanup_user_login(user_id, state)
        await reply_premium(message, "Cancelled.", reply_markup=kb)
        return

    if text.startswith("Remove Profile "):
        try:
            idx = int(text.split(" ")[2]) - 1
            if 0 <= idx < len(state["sessions"]):
                state["sessions"].pop(idx)
                await reply_premium(message, "Account removed.\n\n" + get_status_text(user_id), reply_markup=kb)
            else:
                await reply_premium(message, "Invalid selection.", reply_markup=kb)
        except Exception as e:
            logger.warning(f"[remove_account] Parse error for user {user_id}: {e}")
            await reply_premium(message, "Action failed.", reply_markup=kb)
        return

    # ---- sequential inputs ----
    current_action = state["waiting_for"]
    if not current_action:
        return

    if current_action == "delay":
        try:
            val = int(text)
            if val < 1:
                await reply_premium(message, "Delay must be at least 1 second.")
                return
            state["delay"] = val
            state["waiting_for"] = None
            await reply_premium(message, f"Delay set to {val}s.\n\n" + get_status_text(user_id), reply_markup=kb)
        except ValueError:
            await reply_premium(message, "Numbers only please.")
        return

    if current_action == "message":
        state["message"] = message.text
        state["waiting_for"] = None
        await reply_premium(message, "Message updated.\n\n" + get_status_text(user_id), reply_markup=kb)
        return

    if current_action == "sch_daily":
        canon = parse_hhmm(text)
        if not canon:
            await reply_premium(message, "Invalid time. Use `HH:MM` (24h). Example: `14:30`.")
            return
        add_daily_schedule(state, canon)
        state["waiting_for"] = None
        await reply_premium(message, f"Daily schedule set for `{canon} UTC`.", reply_markup=kb)
        await reply_premium(message, get_schedules_text(user_id), reply_markup=get_schedules_inline(user_id))
        return

    if current_action == "sch_interval":
        val = parse_interval_minutes(text)
        if not val:
            await reply_premium(message, "Invalid. Send a number from 1 to 1440.")
            return
        add_interval_schedule(state, val)
        state["waiting_for"] = None
        await reply_premium(message, f"Interval set — every `{val}` min.", reply_markup=kb)
        await reply_premium(message, get_schedules_text(user_id), reply_markup=get_schedules_inline(user_id))
        return

    if current_action == "phone":
        phone_number = text.replace(" ", "")
        status_msg = await message.reply_text("Connecting...")
        temp_client = Client(
            f"temp_auth_{user_id}",
            api_id=API_ID, api_hash=API_HASH, in_memory=True,
        )
        try:
            await temp_client.connect()
            sent_code = await temp_client.send_code(phone_number)
            state["login_data"] = {
                "phone":           phone_number,
                "phone_code_hash": sent_code.phone_code_hash,
                "client":          temp_client,
                "current_otp":     "",
            }
            state["waiting_for"] = "otp"
            otp_display = format_otp_display("")
            await safe_edit_text(
                status_msg,
                f"**Enter OTP for {phone_number}**\n\n`[ {otp_display} ]`",
                reply_markup=get_otp_inline_keyboard(),
                min_interval=0,
            )
        except Exception as e:
            logger.error(f"[phone_handler] send_code failed for {phone_number}: {e}")
            try:
                await temp_client.disconnect()
            except Exception:
                pass
            state["waiting_for"] = None
            await status_msg.edit_text("Login failed. Check the phone number.")
            await send_premium(user_id, "Back to panel.", reply_markup=kb)
        return

    if current_action == "password":
        status_msg = await message.reply_text("Verifying 2FA...")
        login_data = state["login_data"]
        temp_client = login_data["client"]
        try:
            await temp_client.check_password(text)
            string_session = await temp_client.export_session_string()
            state["sessions"].append(string_session)

            await temp_client.disconnect()
            state["waiting_for"] = None
            state["login_data"] = {}
            await safe_edit_text(status_msg, "**Account added.**\n\n" + get_status_text(user_id), min_interval=0)
            await send_premium(user_id, get_status_text(user_id), reply_markup=kb)
        except Exception as e:
            logger.warning(f"[password_handler] 2FA failed for user {user_id}: {e}")
            try:
                await temp_client.disconnect()
            except Exception:
                pass
            state["waiting_for"] = None
            state["login_data"] = {}
            await safe_edit_text(status_msg, "**Wrong 2FA password.**", min_interval=0)
            await send_premium(user_id, "Back to panel.", reply_markup=kb)
        return
