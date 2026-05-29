import asyncio
import json
import logging
import os
from datetime import datetime, timezone

from telegram import User

logger = logging.getLogger(__name__)

USERS_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "users.json")
_lock = asyncio.Lock()


async def update_user(user: User) -> bool:
    """Update users.json. Returns True if this is the user's first ever message."""
    async with _lock:
        try:
            with open(USERS_PATH, encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            data = {}

        uid = str(user.id)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        if uid not in data:
            data[uid] = {
                "user_id": user.id,
                "username": user.username,
                "first_name": user.first_name,
                "first_seen": now,
                "last_seen": now,
            }
            logger.info("Новый пользователь: %s (%s)", user.id, user.username)
            is_new = True
        else:
            data[uid]["last_seen"] = now
            data[uid]["username"] = user.username
            data[uid]["first_name"] = user.first_name
            is_new = False

        with open(USERS_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        return is_new


async def set_source(user_id: int, source: str) -> None:
    """Set source field for user in users.json (e.g. 'email')."""
    async with _lock:
        try:
            with open(USERS_PATH, encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            data = {}
        uid = str(user_id)
        if uid in data and not data[uid].get("source"):
            data[uid]["source"] = source
            with open(USERS_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            logger.info("Источник пользователя %s: %s", user_id, source)


async def update_dialog_stats(user_id: int, articles: list, amount: float) -> None:
    """Update articles list, total_amount and dialogs_count after dialog ends."""
    async with _lock:
        try:
            with open(USERS_PATH, encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            data = {}
        uid = str(user_id)
        if uid not in data:
            return
        existing = data[uid].get("articles", [])
        for art in articles:
            if art not in existing:
                existing.append(art)
        data[uid]["articles"] = existing
        data[uid]["total_amount"] = round(data[uid].get("total_amount", 0.0) + amount, 2)
        data[uid]["dialogs_count"] = data[uid].get("dialogs_count", 0) + 1
        with open(USERS_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        logger.info("Диалог завершён для %s: +%s артикулов, +%.0f руб", user_id, len(articles), amount)


async def get_all_users() -> dict:
    """Return all users dict from users.json."""
    async with _lock:
        try:
            with open(USERS_PATH, encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}


async def mark_request(user_id: int, text: str) -> None:
    """Record that user sent a request; mark as unresponded."""
    async with _lock:
        try:
            with open(USERS_PATH, encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            data = {}
        uid = str(user_id)
        if uid not in data:
            return
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        data[uid]["last_request_time"] = now
        data[uid]["last_request_text"] = text[:500]
        data[uid]["responded"] = False
        data[uid].pop("last_alert_sent", None)
        # Reset time-based touchpoints on new request; keep monthly ones
        existing = data[uid].get("touchpoints", [])
        data[uid]["touchpoints"] = [tp for tp in existing if tp.startswith("monday_")]
        data[uid].pop("touchpoint_price_snapshot", None)
        with open(USERS_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)


async def mark_responded(user_id: int) -> None:
    """Mark user's last request as responded."""
    async with _lock:
        try:
            with open(USERS_PATH, encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            data = {}
        uid = str(user_id)
        if uid not in data:
            return
        data[uid]["responded"] = True
        with open(USERS_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)


async def get_unresponded_users() -> list:
    """Return list of users with responded=False and a recorded request."""
    async with _lock:
        try:
            with open(USERS_PATH, encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return []
    result = []
    for uid, u in data.items():
        if not u.get("responded", True) and u.get("last_request_time"):
            result.append({**u, "_uid": uid})
    return result


async def add_touchpoint(user_id: int, tp_type: str) -> None:
    """Append touchpoint type to user's touchpoints list (idempotent)."""
    async with _lock:
        try:
            with open(USERS_PATH, encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            data = {}
        uid = str(user_id)
        if uid not in data:
            return
        tps = data[uid].get("touchpoints", [])
        if tp_type not in tps:
            tps.append(tp_type)
            data[uid]["touchpoints"] = tps
            with open(USERS_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)


async def save_price_snapshot(user_id: int, article: str, price: str) -> None:
    """Save article price snapshot for day3 touchpoint comparison."""
    async with _lock:
        try:
            with open(USERS_PATH, encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            data = {}
        uid = str(user_id)
        if uid not in data:
            return
        data[uid]["touchpoint_price_snapshot"] = {"article": article, "price": price}
        with open(USERS_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)


async def update_last_alert(user_id: int) -> None:
    """Update last_alert_sent timestamp after manager alert is sent."""
    async with _lock:
        try:
            with open(USERS_PATH, encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            data = {}
        uid = str(user_id)
        if uid not in data:
            return
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        data[uid]["last_alert_sent"] = now
        with open(USERS_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
