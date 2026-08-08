"""Premium custom-emoji entity helpers."""
from pyrogram.types import MessageEntity
from pyrogram.enums import MessageEntityType
from config import PREMIUM_EMOJI


def premium_entities(text: str):
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


def premium_kwargs(text: str) -> dict:
    return {"entities": premium_entities(text)}
