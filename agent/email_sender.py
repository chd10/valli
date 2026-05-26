import asyncio
import logging
import os
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

logger = logging.getLogger(__name__)

SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.yandex.ru")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))
SMTP_USER = os.environ["SMTP_USER"]
SMTP_PASS = os.environ["SMTP_PASS"]
EMAIL_TO = os.environ.get("EMAIL_TO", "chd10@ya.ru")


def _format_history(history: list[dict]) -> str:
    lines = []
    for msg in history:
        role = "Клиент" if msg["role"] == "user" else "Валли"
        lines.append(f"{role}:\n{msg['content'].strip()}\n")
    return "\n".join(lines)


def _send_sync(user_label: str, history: list[dict]) -> None:
    date_str = datetime.now().strftime("%d.%m.%Y")
    subject = f"Валли: чат с {user_label} {date_str}"
    body = (
        f"История чата с {user_label}\n"
        f"{date_str}\n\n"
        f"{'=' * 50}\n\n"
        f"{_format_history(history)}"
    )

    msg = MIMEMultipart()
    msg["From"] = SMTP_USER
    msg["To"] = EMAIL_TO
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))

    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as server:
        server.login(SMTP_USER, SMTP_PASS)
        server.send_message(msg)

    logger.info("Email отправлен: «%s» → %s", subject, EMAIL_TO)


async def send_chat_history(user_label: str, history: list[dict]) -> None:
    if not history:
        return
    await asyncio.to_thread(_send_sync, user_label, history)
