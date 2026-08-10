"""Broadcast scheduler — fires user-defined daily and interval schedules."""
import asyncio
import time
import uuid
from datetime import datetime, timezone

from config import logger
from state import USER_STATES
from sender import run_scheduled_broadcast


def _new_id() -> str:
    return "sch_" + uuid.uuid4().hex[:6]


def add_daily_schedule(state: dict, time_str: str, message: str | None = None) -> dict:
    sch = {
        "id":            _new_id(),
        "kind":          "daily",
        "time":          time_str,       # "HH:MM" (UTC)
        "message":       message,
        "enabled":       True,
        "last_run_key":  "",
    }
    state["schedules"].append(sch)
    return sch


def add_interval_schedule(state: dict, interval_minutes: int, message: str | None = None) -> dict:
    sch = {
        "id":               _new_id(),
        "kind":             "interval",
        "interval_minutes": interval_minutes,
        "message":          message,
        "enabled":          True,
        "last_run_ts":      0.0,
    }
    state["schedules"].append(sch)
    return sch


def remove_schedule(state: dict, sch_id: str) -> bool:
    before = len(state["schedules"])
    state["schedules"] = [s for s in state["schedules"] if s["id"] != sch_id]
    return len(state["schedules"]) < before


def toggle_schedule(state: dict, sch_id: str) -> bool:
    for s in state["schedules"]:
        if s["id"] == sch_id:
            s["enabled"] = not s.get("enabled", True)
            return True
    return False


def parse_hhmm(text: str) -> str | None:
    """Return canonical 'HH:MM' string or None if invalid."""
    try:
        parts = text.strip().split(":")
        if len(parts) != 2:
            return None
        h, m = int(parts[0]), int(parts[1])
        if 0 <= h < 24 and 0 <= m < 60:
            return f"{h:02d}:{m:02d}"
    except ValueError:
        pass
    return None


def parse_interval_minutes(text: str) -> int | None:
    try:
        val = int(text.strip())
        if 1 <= val <= 24 * 60:
            return val
    except ValueError:
        pass
    return None


async def scheduler_loop():
    """Poll every 20s and fire due schedules for every registered user."""
    logger.info("[Scheduler] Loop started.")
    while True:
        try:
            now_utc = datetime.now(timezone.utc)
            now_ts = time.time()
            minute_key = now_utc.strftime("%Y-%m-%d %H:%M")
            hhmm_now = now_utc.strftime("%H:%M")

            for user_id, state in list(USER_STATES.items()):
                for sch in list(state.get("schedules", [])):
                    if not sch.get("enabled"):
                        continue
                    fire = False
                    if sch["kind"] == "daily":
                        if hhmm_now == sch.get("time") and sch.get("last_run_key") != minute_key:
                            sch["last_run_key"] = minute_key
                            fire = True
                    elif sch["kind"] == "interval":
                        gap = sch["interval_minutes"] * 60
                        last = sch.get("last_run_ts", 0.0)
                        if now_ts - last >= gap:
                            sch["last_run_ts"] = now_ts
                            fire = True
                    if fire:
                        logger.info(f"[Scheduler] Firing {sch['id']} for user {user_id}")
                        asyncio.create_task(run_scheduled_broadcast(user_id, sch.get("message")))
        except Exception as e:
            logger.error(f"[Scheduler] Loop error: {e}", exc_info=True)
        await asyncio.sleep(20)
