"""Pure error classifier for Pyrogram send/forward failures.

No I/O, no imports from the rest of the bot — deterministic and unit-testable.
Turns any raised exception into a single Decision the send loop acts on.
"""
from enum import Enum
from dataclasses import dataclass


class Action(Enum):
    RETRY        = "retry"
    BACKOFF      = "backoff"        # FloodWait: wait N secs (this account only)
    SKIP_GROUP   = "skip_group"     # remember (account, chat) ban forever
    DISABLE_ACCT = "disable_acct"   # frozen/banned account: pull out for good


@dataclass
class Decision:
    action: Action
    reason: str
    wait_seconds: int = 0


# Pyrogram RPCError exposes: .CODE (int), .ID (str), .NAME, .MESSAGE; FloodWait has .value (secs)
def _sig(err) -> tuple[int, str, int]:
    code = int(getattr(err, "CODE", 0) or 0)
    ident = (getattr(err, "ID", None) or getattr(err, "NAME", None)
             or type(err).__name__)
    sig = f"{ident} {err}".upper()
    wait = getattr(err, "value", None)
    wait = int(wait) if isinstance(wait, (int, float)) else 0
    return code, sig, wait


DISABLE = ("FROZEN_METHOD_INVALID", "USER_DEACTIVATED", "USER_DEACTIVATED_BAN",
           "AUTH_KEY_UNREGISTERED", "AUTH_KEY_DUPLICATED", "SESSION_REVOKED",
           "ACCOUNT_BANNED")
SKIP    = ("USER_BANNED_IN_CHANNEL", "CHAT_WRITE_FORBIDDEN", "CHANNEL_PRIVATE",
           "CHAT_ADMIN_REQUIRED", "USER_KICKED", "PEER_ID_INVALID",
           "CHAT_SEND_PLAIN_FORBIDDEN", "CHANNEL_BANNED")


def classify(err) -> Decision:
    code, sig, wait = _sig(err)
    if any(k in sig for k in DISABLE):                       # check FIRST — 420 frozen != floodwait
        return Decision(Action.DISABLE_ACCT, "account_frozen_or_banned")
    if "FLOOD_WAIT" in sig or "SLOWMODE_WAIT" in sig:
        return Decision(Action.BACKOFF, "flood_wait", wait or 10)
    if any(k in sig for k in SKIP):
        return Decision(Action.SKIP_GROUP, "banned_or_forbidden_in_chat")
    if code in (401, 403):
        return Decision(Action.DISABLE_ACCT, "auth_dead")
    return Decision(Action.RETRY, "transient")
