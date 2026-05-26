import asyncio
import json
import logging
import os
from datetime import datetime, timezone

from telegram import User

logger = logging.getLogger(__name__)

USERS_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "users.json")
_lock = asyncio.Lock()


async def update_user(user: User) -> None:
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
        else:
            data[uid]["last_seen"] = now
            data[uid]["username"] = user.username
            data[uid]["first_name"] = user.first_name

        with open(USERS_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
