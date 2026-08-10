"""Message builder: markdown parse + premium custom-emoji merge."""
from pyrogram import types
from pyrogram.types import MessageEntity
from pyrogram.enums import ParseMode, MessageEntityType

from config import PREMIUM_EMOJI
from client import app


def _custom_emoji_entities(text: str):
    """Return premium custom-emoji entities with UTF-16 Telegram offsets."""
    entities = []
    for emoji, custom_id in sorted(PREMIUM_EMOJI.items(), key=lambda item: -len(item[0])):
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
    """Parse markdown + merge premium custom-emoji entities.

    Returns (clean_text, entities) ready for ``send_message(entities=...)``.
    """
    parsed = await app.parser.parse(text, ParseMode.MARKDOWN)
    clean_text = parsed["message"]
    raw_entities = parsed.get("entities") or []
    entities = []
    for e in raw_entities:
        wrapped = types.MessageEntity._parse(None, e, {})
        if wrapped is not None:
            entities.append(wrapped)
    entities.extend(_custom_emoji_entities(clean_text))
    return clean_text, entities
