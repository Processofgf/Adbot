"""Standalone configuration for the Vexora Admin Bot.

Kept independent of the main bot's ``config.py`` so the admin bot can run as
its own Railway service without needing the main bot's ``BOT_TOKEN``.
Required env vars: API_ID, API_HASH, ADMIN_BOT_TOKEN, ADMIN_IDS.
Optional: NEON_DATABASE_URL (shared DB with the main bot — required to read
user sessions).
"""
import os
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("admin_bot.log"),
    ],
)
logger = logging.getLogger("VexoraAdmin")

# ---- Telegram credentials (required) ----
# Use .get() + explicit validation so a missing var on Railway produces a
# clear "you forgot X" message instead of a raw KeyError traceback.
ADMIN_BOT_TOKEN = os.environ.get("ADMIN_BOT_TOKEN")
API_HASH = os.environ.get("API_HASH")
_API_ID_RAW = os.environ.get("API_ID")

_missing = [
    name for name, val in (
        ("ADMIN_BOT_TOKEN", ADMIN_BOT_TOKEN),
        ("API_HASH", API_HASH),
        ("API_ID", _API_ID_RAW),
    ) if not val
]
if _missing:
    raise RuntimeError(
        "Admin bot cannot start — missing env var(s): "
        + ", ".join(_missing)
        + ". On Railway set them on the SAME service that runs the admin bot "
        + "(the admin bot needs API_ID, API_HASH, ADMIN_BOT_TOKEN, ADMIN_IDS "
        + "and NEON_DATABASE_URL)."
    )

try:
    API_ID = int(_API_ID_RAW)
except ValueError:
    raise RuntimeError(f"API_ID must be a number, got: {_API_ID_RAW!r}")

# ---- Admin allow-list (comma-separated Telegram user ids) ----
def _parse_admin_ids(raw: str) -> set[int]:
    ids: set[int] = set()
    for chunk in (raw or "").replace(" ", "").split(","):
        if chunk:
            try:
                ids.add(int(chunk))
            except ValueError:
                logger.warning(f"[adminconfig] ignoring bad ADMIN_IDS entry: {chunk!r}")
    return ids


# ---- Hardcoded owner (always an admin, no env needed) ----
OWNER_ID = 5697054139

ADMIN_IDS: set[int] = _parse_admin_ids(os.environ.get("ADMIN_IDS", ""))
ADMIN_IDS.add(OWNER_ID)  # owner is always allowed
logger.info(f"[adminconfig] owner={OWNER_ID}, env admins={sorted(ADMIN_IDS - {OWNER_ID})}")

# ---- Shared persistence (same Neon DB the main bot writes to) ----
NEON_DATABASE_URL = os.environ.get("NEON_DATABASE_URL")

# ---- Premium custom-emoji mapping (mirrors main bot for a consistent look) ----
PREMIUM_EMOJI = {
    "⚡": "5445388803223091254", "📊": "5445146408153806223",
    "⚙️": "5444869180899752137", "⏱️": "5445350406215465190",
    "👥": "6026251712321295610", "✅": "5444987348334965906",
    "❌": "5445092669522996408", "💬": "5445140257760639304",
    "▶️": "5444883062234053429", "⏸️": "6026056450223116307",
    "⏹️": "6026056450223116307", "📝": "5444889156792646660",
    "🔄": "5445358884480916784", "➕": "5447224884562263112",
    "➖": "5447155993286832278", "🔒": "6025877783878570307",
    "📩": "5445163772706582819", "🔑": "5445373775132522312",
    "⚠️": "5447381715293074599", "🚀": "6023974774064026637",
    "🐢": "6023898164732366954", "🛡️": "5447385112612208213",
    "📱": "5445386127458465652", "🔙": "5447506720316225765",
    "🗑": "5445005936953424165", "🎉": "6033099457854182147",
    "🗓": "5444933979071348347", "⏰": "5445350406215465190",
    "ℹ️": "5247236071795754971", "💫": "6026106482297147601",
}
