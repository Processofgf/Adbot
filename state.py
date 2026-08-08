"""In-memory user state, running-task registry and persistence hook."""
import asyncio
from config import logger

USER_STATES: dict[int, dict] = {}
RUNNING_TASKS: dict[int, asyncio.Task] = {}

# Resolved chat IDs of the auto-join Vexora channels. Populated by the
# enforcer whenever it (re)joins or resolves one of the invite links, so
# broadcast sends can skip them. Pre-seeded with known Vexora GC ids that
# should always be excluded, regardless of whether dynamic
# join/check_chat_invite resolution succeeds for a given account.
VEXORA_CHAT_IDS: set[int] = {
    -1003729050149,
}


DEFAULT_STATE = lambda: {
    "status":       "STOPPED",
    "sending_mode": "NORMAL",
    "delay":        15,
    "message":      "Your Custom Promo Message Here",
    "sessions":     [],
    "sent_count":   0,
    "failed_count": 0,
    "waiting_for":  None,
    "login_data":   {},
    "schedules":    [],
}


def initialize_user_state(user_id: int) -> dict:
    if user_id not in USER_STATES:
        USER_STATES[user_id] = DEFAULT_STATE()
    st = USER_STATES[user_id]
    st.setdefault("schedules", [])
    st.setdefault("login_data", {})
    st.setdefault("waiting_for", None)
    return st


async def cleanup_user_login(user_id: int, state: dict):
    if "client" in state["login_data"]:
        try:
            await state["login_data"]["client"].disconnect()
        except Exception as e:
            logger.debug(f"[cleanup_user_login] Disconnect error user {user_id}: {e}")
    state["login_data"] = {}


async def persist(user_id: int):
    """Save state to Neon (no-op if DB not configured)."""
    from db import save_state
    st = USER_STATES.get(user_id)
    if st is not None:
        await save_state(user_id, st)


def persist_bg(user_id: int):
    """Fire-and-forget persist — safe from sync contexts inside coroutines."""
    try:
        asyncio.create_task(persist(user_id))
    except RuntimeError:
        pass  # no running loop; ignore
