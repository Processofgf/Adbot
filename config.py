"""Configuration, logging, brand constants and premium emoji ID mapping."""
import os
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot_errors.log"),
    ],
)
logger = logging.getLogger("VexoraAds")

# ---- Telegram credentials (required) ----
BOT_TOKEN = os.environ["BOT_TOKEN"]
API_HASH = os.environ["API_HASH"]
API_ID = int(os.environ["API_ID"])

if not API_ID or not API_HASH or not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN, API_ID and API_HASH must be set")

# ---- Persistence (optional). Set on Railway to enable. ----
NEON_DATABASE_URL = os.environ.get("NEON_DATABASE_URL")

# ---- Brand ----
VEXORA_BOT_USERNAME = "@VexoraAdsBot"
VEXORA_BIO = "Ads Powered By @VexoraAdsBot 💫"
VEXORA_NAME_SUFFIX = " @VexoraAdsBot"
VEXORA_CHANNELS = [
    "https://t.me/+RMD_w7F47OA4OTVl",
    "https://t.me/+JZXrkDbxpb9jMTNl",
]

# ---- Premium custom-emoji mapping ----
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
