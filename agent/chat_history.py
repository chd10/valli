import os
import json
import datetime as dt

CHATS_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "chats")


def _ensure_dir() -> None:
    os.makedirs(CHATS_DIR, exist_ok=True)


def _chat_path(user_id: int, username: str | None) -> str:
    safe = (username or "noname").replace("/", "_").replace("\\", "_")
    return os.path.join(CHATS_DIR, f"{user_id}_{safe}.json")


def _find_path(user_id: int) -> str | None:
    _ensure_dir()
    prefix = f"{user_id}_"
    for name in os.listdir(CHATS_DIR):
        if name.startswith(prefix) and name.endswith(".json"):
            return os.path.join(CHATS_DIR, name)
    return None


def is_new_user(user_id: int) -> bool:
    return _find_path(user_id) is None


def append_message(
    user_id: int,
    username: str | None,
    first_name: str | None,
    direction: str,
    text: str,
) -> None:
    _ensure_dir()
    path = _find_path(user_id) or _chat_path(user_id, username)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = {
            "user_id": user_id,
            "username": username,
            "first_name": first_name,
            "messages": [],
        }
    now = dt.datetime.now()
    data["messages"].append({
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M:%S"),
        "direction": direction,
        "text": text,
    })
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_last_messages(user_id: int, n: int = 20) -> list[dict]:
    path = _find_path(user_id)
    if not path:
        return []
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data.get("messages", [])[-n:]
