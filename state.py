"""In-memory user state and running-task registry."""
import asyncio
from config import logger

USER_STATES: dict[int, dict] = {}
RUNNING_TASKS: dict[int, asyncio.Task] = {}


def initialize_user_state(user_id: int) -> dict:
    if user_id not in USER_STATES:
        USER_STATES[user_id] = {
            "status":       "STOPPED",
            "sending_mode": "NORMAL",
            "delay":        15,
            "message":      "🔥 Your Custom Promo Message Here 🔥",
            "sessions":     [],
            "sent_count":   0,
            "failed_count": 0,
            "waiting_for":  None,
            "login_data":   {},
            "schedules":    [],
        }
    # Migration for older state dicts
    USER_STATES[user_id].setdefault("schedules", [])
    return USER_STATES[user_id]


async def cleanup_user_login(user_id: int, state: dict):
    if "client" in state["login_data"]:
        try:
            await state["login_data"]["client"].disconnect()
        except Exception as e:
            logger.debug(f"[cleanup_user_login] Disconnect error user {user_id}: {e}")
    state["login_data"] = {}
