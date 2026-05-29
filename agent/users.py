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
