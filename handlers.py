"""Telegram command / message / callback handlers."""
import asyncio

from pyrogram import filters, Client
from pyrogram.errors import SessionPasswordNeeded

from client import app
from config import API_ID, API_HASH, logger
from premium import premium_kwargs
from state import (
    USER_STATES, RUNNING_TASKS, initialize_user_state, cleanup_user_login,
)
from ui import (
    get_premium_keyboard, cancel_keyboard, styled_button, get_status_text,
    get_otp_inline_keyboard, format_otp_display, safe_edit_text,
    get_schedules_inline, get_schedules_text,
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
        welcome = (
            "✨ **Welcome to your Control Room**\n\n"
            "Manage your connected accounts, delivery mode and promo message "
            "from one clean panel. Everything you need is right below.\n\n"
            "💬 Choose an action to get started."
        )
        await message.reply_text(
            welcome,
            reply_markup=get_premium_keyboard(state["sending_mode"]),
            **premium_kwargs(welcome),
        )
    else:
        text = get_status_text(user_id)
        await message.reply_text(
            text,
            reply_markup=get_premium_keyboard(state["sending_mode"]),
            **premium_kwargs(text),
        )


# ==================== OTP INLINE KEYPAD ====================
@app.on_callback_query(filters.regex(r"^otp_key:"))
async def otp_keypad_handler(client, callback_query):
    user_id = callback_query.from_user.id
    state = initialize_user_state(user_id)
    action = callback_query.data.split(":")[1]

    if state["waiting_for"] != "otp" or "client" not in state["login_data"]:
        await callback_query.answer("⚠️ Session expired.", show_alert=True)
        return

    login_data = state["login_data"]
    current_otp = login_data.get("current_otp", "")

    if action == "cancel":
        await callback_query.answer("Cancelled")
        state["waiting_for"] = None
        await cleanup_user_login(user_id, state)
        await safe_edit_text(callback_query.message, "❌ **Login cancelled.**", min_interval=0)
        reply = "Returned to menu."
        await app.send_message(
            user_id, reply,
            reply_markup=get_premium_keyboard(state["sending_mode"]),
            **premium_kwargs(reply),
        )
        return

    elif action == "del":
        if current_otp:
            current_otp = current_otp[:-1]
            login_data["current_otp"] = current_otp
            otp_display = format_otp_display(current_otp)
            await safe_edit_text(
                callback_query.message,
                f"📩 **Enter 5-digit OTP for {login_data['phone']}:**\n\n🔑 **[ {otp_display} ]**",
                reply_markup=get_otp_inline_keyboard(),
                min_interval=0.3,
            )
        await callback_query.answer()
        return

    elif action == "submit":
        if len(current_otp) < 5:
            await callback_query.answer("⚠️ Enter all 5 digits!", show_alert=True)
            return
        await process_otp_login(client, callback_query.message, user_id, state, current_otp)
        await callback_query.answer()
        return

    else:
        if len(current_otp) < 5:
            current_otp += action
            login_data["current_otp"] = current_otp
            otp_display = format_otp_display(current_otp)

            if len(current_otp) == 5:
                await safe_edit_text(
                    callback_query.message,
                    f"⏳ **Verifying OTP Code...**\n\n🔑 **[ {otp_display} ]**",
                    reply_markup=None,
                    min_interval=0,
                )
                await callback_query.answer("Verifying...")
                await process_otp_login(client, callback_query.message, user_id, state, current_otp)
                return
            else:
                await safe_edit_text(
                    callback_query.message,
                    f"📩 **Enter 5-digit OTP for {login_data['phone']}:**\n\n🔑 **[ {otp_display} ]**",
                    reply_markup=get_otp_inline_keyboard(),
                    min_interval=0.2,
                )
                await callback_query.answer()
                return


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
        await safe_edit_text(message, "✅ **Account successfully added!**", min_interval=0)
        status = get_status_text(user_id)
        await app.send_message(
            user_id, status,
            reply_markup=get_premium_keyboard(state["sending_mode"]),
            **premium_kwargs(status),
        )

    except SessionPasswordNeeded:
        state["waiting_for"] = "password"
        await safe_edit_text(message, "🔒 **2FA Password Required:**\nEnter your Cloud Password in chat:", min_interval=0)

    except Exception as e:
        logger.warning(f"[process_otp_login] OTP failed for user {user_id}: {e}")
        try:
            await temp_client.disconnect()
        except Exception:
            pass
        state["waiting_for"] = None
        state["login_data"] = {}
        await safe_edit_text(message, "❌ **Invalid OTP Code!**", min_interval=0)
        reply = "Login failed. Returned to menu."
        await app.send_message(
            user_id, reply,
            reply_markup=get_premium_keyboard(state["sending_mode"]),
            **premium_kwargs(reply),
        )


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
            await callback_query.answer("Toggled ✓")
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
            await callback_query.answer("Removed 🗑")
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
            prompt = (
                "⏰ **Add Daily Schedule**\n"
                "Send time in `HH:MM` (24h, **UTC**) — e.g. `14:30`."
            )
            await app.send_message(
                user_id, prompt,
                reply_markup=cancel_keyboard(),
                **premium_kwargs(prompt),
            )
        else:
            state["waiting_for"] = "sch_interval"
            await callback_query.answer()
            prompt = (
                "⏱️ **Add Interval Schedule**\n"
                "Send interval in **minutes** (1 – 1440) — e.g. `60`."
            )
            await app.send_message(
                user_id, prompt,
                reply_markup=cancel_keyboard(),
                **premium_kwargs(prompt),
            )
        return


# ==================== TEXT / KEYBOARD HANDLERS ====================
@app.on_message(filters.text & filters.private)
async def user_text_handler(client, message):
    user_id = message.from_user.id
    text = message.text.strip()
    state = initialize_user_state(user_id)

    # ---- Keyboard buttons ----
    if text == "🔄 Refresh Status":
        state["waiting_for"] = None
        await cleanup_user_login(user_id, state)
        status = get_status_text(user_id)
        await message.reply_text(
            status, reply_markup=get_premium_keyboard(state["sending_mode"]),
            **premium_kwargs(status),
        )
        return

    if text == "▶️ RUN":
        if state["status"] == "RUNNING":
            reply = "✨ Engine is already running!"
            await message.reply_text(reply, reply_markup=get_premium_keyboard(state["sending_mode"]), **premium_kwargs(reply))
            return
        state["status"] = "RUNNING"
        if user_id not in RUNNING_TASKS or RUNNING_TASKS[user_id].done():
            RUNNING_TASKS[user_id] = asyncio.create_task(dedicated_user_worker(user_id))
        reply = "🚀 Engine started successfully!"
        await message.reply_text(reply, reply_markup=get_premium_keyboard(state["sending_mode"]), **premium_kwargs(reply))
        return

    if text == "⏸️ PAUSE":
        state["status"] = "PAUSED"
        reply = "⏸️ Engine paused."
        await message.reply_text(reply, reply_markup=get_premium_keyboard(state["sending_mode"]), **premium_kwargs(reply))
        return

    if text == "⏹️ STOP":
        state["status"] = "STOPPED"
        state["sent_count"] = 0
        state["failed_count"] = 0
        if user_id in RUNNING_TASKS and not RUNNING_TASKS[user_id].done():
            RUNNING_TASKS[user_id].cancel()
        reply = "⏹️ Engine stopped. Metrics reset."
        await message.reply_text(reply, reply_markup=get_premium_keyboard(state["sending_mode"]), **premium_kwargs(reply))
        return

    if text in ["🐢 Mode: NORMAL", "🚀 Mode: ADVANCED"]:
        state["sending_mode"] = "ADVANCED" if state["sending_mode"] == "NORMAL" else "NORMAL"
        status = get_status_text(user_id)
        await message.reply_text(status, reply_markup=get_premium_keyboard(state["sending_mode"]), **premium_kwargs(status))
        return

    if text == "⏱️ Set Delay":
        state["waiting_for"] = "delay"
        prompt = "⏱️ **Enter delay interval in seconds (e.g. 15):**"
        await message.reply_text(prompt, reply_markup=cancel_keyboard(), **premium_kwargs(prompt))
        return

    if text == "📝 Set Message":
        state["waiting_for"] = "message"
        prompt = "📝 **Send your custom promo message:**"
        await message.reply_text(prompt, reply_markup=cancel_keyboard(), **premium_kwargs(prompt))
        return

    if text == "➕ Add Account":
        state["waiting_for"] = "phone"
        prompt = "➕ **Send Account Phone Number (with country code):**\nExample: `+1234567890`"
        await message.reply_text(prompt, reply_markup=cancel_keyboard(), **premium_kwargs(prompt))
        return

    if text == "➖ Remove Account":
        if not state["sessions"]:
            await message.reply_text("❌ No active accounts found!", reply_markup=get_premium_keyboard(state["sending_mode"]))
            return
        from pyrogram.types import ReplyKeyboardMarkup
        buttons = [[styled_button(f"❌ Remove Profile {idx+1}", "5445092669522996408", "red")] for idx in range(len(state["sessions"]))]
        buttons.append([styled_button("🔙 Cancel Operational Mode", "5447506720316225765", "blue")])
        prompt = "➖ **Select account to remove:**"
        await message.reply_text(prompt, reply_markup=ReplyKeyboardMarkup(buttons, resize_keyboard=True), **premium_kwargs(prompt))
        return

    if text == "🗓 Schedules":
        state["waiting_for"] = None
        body = get_schedules_text(user_id)
        await message.reply_text(
            body,
            reply_markup=get_schedules_inline(user_id),
            **premium_kwargs(body),
        )
        return

    if text == "🔙 Cancel Operational Mode":
        state["waiting_for"] = None
        await cleanup_user_login(user_id, state)
        await message.reply_text("🔄 Cancelled. Returned to menu.", reply_markup=get_premium_keyboard(state["sending_mode"]))
        return

    if text.startswith("❌ Remove Profile "):
        try:
            idx = int(text.split(" ")[3]) - 1
            if 0 <= idx < len(state["sessions"]):
                state["sessions"].pop(idx)
                await message.reply_text(
                    "✅ Account removed successfully!\n\n" + get_status_text(user_id),
                    reply_markup=get_premium_keyboard(state["sending_mode"]),
                )
            else:
                await message.reply_text("❌ Invalid selection.", reply_markup=get_premium_keyboard(state["sending_mode"]))
        except Exception as e:
            logger.warning(f"[remove_account] Parse error for user {user_id}: {e}")
            await message.reply_text("❌ Action failed.", reply_markup=get_premium_keyboard(state["sending_mode"]))
        return

    # ---- Sequential inputs ----
    current_action = state["waiting_for"]
    if not current_action:
        return

    if current_action == "delay":
        try:
            val = int(text)
            if val < 1:
                await message.reply_text("❌ Delay must be at least 1 second.")
                return
            state["delay"] = val
            state["waiting_for"] = None
            await message.reply_text(
                f"✅ Delay updated to {val} seconds!\n\n" + get_status_text(user_id),
                reply_markup=get_premium_keyboard(state["sending_mode"]),
            )
        except ValueError:
            await message.reply_text("❌ Numbers only please.")
        return

    if current_action == "message":
        state["message"] = message.text
        state["waiting_for"] = None
        await message.reply_text(
            "✅ Promo message saved!\n\n" + get_status_text(user_id),
            reply_markup=get_premium_keyboard(state["sending_mode"]),
        )
        return

    if current_action == "sch_daily":
        canon = parse_hhmm(text)
        if not canon:
            await message.reply_text("❌ Invalid time. Use `HH:MM` (24h, e.g. `14:30`).")
            return
        sch = add_daily_schedule(state, canon)
        state["waiting_for"] = None
        confirm = f"✅ **Daily schedule added** — fires at `{canon} UTC`."
        await message.reply_text(confirm, reply_markup=get_premium_keyboard(state["sending_mode"]), **premium_kwargs(confirm))
        body = get_schedules_text(user_id)
        await message.reply_text(body, reply_markup=get_schedules_inline(user_id), **premium_kwargs(body))
        return

    if current_action == "sch_interval":
        val = parse_interval_minutes(text)
        if not val:
            await message.reply_text("❌ Invalid interval. Send a number between 1 and 1440.")
            return
        sch = add_interval_schedule(state, val)
        state["waiting_for"] = None
        confirm = f"✅ **Interval schedule added** — every `{val}` min."
        await message.reply_text(confirm, reply_markup=get_premium_keyboard(state["sending_mode"]), **premium_kwargs(confirm))
        body = get_schedules_text(user_id)
        await message.reply_text(body, reply_markup=get_schedules_inline(user_id), **premium_kwargs(body))
        return

    if current_action == "phone":
        phone_number = text.replace(" ", "")
        status_msg = await message.reply_text("⏳ Connecting to Telegram...")
        temp_client = Client(
            f"temp_auth_{user_id}",
            api_id=API_ID,
            api_hash=API_HASH,
            in_memory=True,
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
                f"📩 **Enter 5-digit OTP for {phone_number}:**\n\n🔑 **[ {otp_display} ]**",
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
            await status_msg.edit_text("❌ **Login Failed! Check phone number.**")
            await app.send_message(user_id, "Returned to menu.", reply_markup=get_premium_keyboard(state["sending_mode"]))
        return

    if current_action == "password":
        status_msg = await message.reply_text("⏳ Verifying 2FA Password...")
        login_data = state["login_data"]
        temp_client = login_data["client"]
        try:
            await temp_client.check_password(text)
            string_session = await temp_client.export_session_string()
            state["sessions"].append(string_session)

            await temp_client.disconnect()
            state["waiting_for"] = None
            state["login_data"] = {}
            await safe_edit_text(
                status_msg,
                "✅ **Account verified and added!**\n\n" + get_status_text(user_id),
                min_interval=0,
            )
            await app.send_message(user_id, get_status_text(user_id), reply_markup=get_premium_keyboard(state["sending_mode"]))
        except Exception as e:
            logger.warning(f"[password_handler] 2FA failed for user {user_id}: {e}")
            try:
                await temp_client.disconnect()
            except Exception:
                pass
            state["waiting_for"] = None
            state["login_data"] = {}
            await safe_edit_text(status_msg, "❌ **Incorrect 2FA Password!**", min_interval=0)
            await app.send_message(user_id, "Returned to menu.", reply_markup=get_premium_keyboard(state["sending_mode"]))
        return
