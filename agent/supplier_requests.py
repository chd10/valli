import json
import os
from datetime import datetime, timezone, timedelta

SUPPLIERS_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "suppliers.json")
PENDING_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "pending_requests.json")

MSK = timezone(timedelta(hours=3))

# Request statuses:
#   pending_selection — waiting for manager to choose suppliers
#   queued            — manager chose, but outside working hours (send at 09:00 MSK)
#   pending           — sent to suppliers, awaiting reply
#   closed            — answered


def moscow_now() -> datetime:
    return datetime.now(MSK)


def is_working_hours() -> bool:
    """True if current Moscow time is 09:00–18:00."""
    return 9 <= moscow_now().hour < 18


def load_suppliers() -> list[dict]:
    with open(SUPPLIERS_FILE) as f:
        return json.load(f)


def load_pending() -> list[dict]:
    if os.path.exists(PENDING_FILE):
        with open(PENDING_FILE) as f:
            return json.load(f)
    return []


def save_pending(requests: list[dict]) -> None:
    os.makedirs(os.path.dirname(PENDING_FILE), exist_ok=True)
    with open(PENDING_FILE, "w") as f:
        json.dump(requests, f, ensure_ascii=False, indent=2)


def get_existing_active(article: str, client_chat_id: int) -> dict | None:
    """Return any non-closed request for this article + client."""
    for req in load_pending():
        if (
            req["article"] == article
            and req["client_chat_id"] == client_chat_id
            and req["status"] != "closed"
        ):
            return req
    return None


def create_request(
    article: str,
    client_chat_id: int,
    client_label: str,
    status: str = "pending_selection",
) -> dict:
    existing = get_existing_active(article, client_chat_id)
    if existing:
        return existing
    pending = load_pending()
    request_id = max((r["id"] for r in pending), default=0) + 1
    req = {
        "id": request_id,
        "article": article,
        "client_chat_id": client_chat_id,
        "client_label": client_label,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "status": status,
        "selected_supplier_ids": [],
    }
    pending.append(req)
    save_pending(pending)
    return req


def set_selected_suppliers(request_id: int, supplier_ids: list[int], queued: bool) -> None:
    """Store chosen suppliers and advance status to queued or pending."""
    pending = load_pending()
    for req in pending:
        if req["id"] == request_id:
            req["selected_supplier_ids"] = supplier_ids
            req["status"] = "queued" if queued else "pending"
            if not queued:
                req["sent_at"] = datetime.now().isoformat(timespec="seconds")
            break
    save_pending(pending)


def mark_pending(request_id: int) -> None:
    """Mark a queued request as pending (sent to suppliers)."""
    pending = load_pending()
    for req in pending:
        if req["id"] == request_id:
            req["status"] = "pending"
            req["sent_at"] = datetime.now().isoformat(timespec="seconds")
            break
    save_pending(pending)


def get_request(request_id: int) -> dict | None:
    for req in load_pending():
        if req["id"] == request_id:
            return req
    return None


def close_request(request_id: int) -> None:
    pending = load_pending()
    for req in pending:
        if req["id"] == request_id:
            req["status"] = "closed"
            req["closed_at"] = datetime.now().isoformat(timespec="seconds")
            break
    save_pending(pending)


def get_supplier_by_chat_id(chat_id: int) -> dict | None:
    try:
        for s in load_suppliers():
            if s["chat_id"] == chat_id:
                return s
    except Exception:
        pass
    return None


def get_all_active() -> list[dict]:
    """All non-closed requests (any status)."""
    return [r for r in load_pending() if r["status"] != "closed"]


def get_sent_to_suppliers() -> list[dict]:
    """Only requests actually sent to suppliers (status=pending)."""
    return [r for r in load_pending() if r["status"] == "pending"]


def get_queued() -> list[dict]:
    """Requests queued for 09:00 MSK send."""
    return [r for r in load_pending() if r["status"] == "queued"]


# keep old name as alias for code that used it
def get_open_pending() -> list[dict]:
    return get_all_active()
