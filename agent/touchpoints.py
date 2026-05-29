"""Scheduler персональных касаний клиентов после запроса."""

import logging
import os
import datetime as dt

from anthropic import AsyncAnthropic
from telegram.ext import ContextTypes

import users
import chat_history
import supplier_requests
from search import search

logger = logging.getLogger(__name__)

MANAGER_CHAT_ID = int(os.environ["MANAGER_CHAT_ID"])

_client = AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

_SYSTEM = (
    "Ты — Валли, робот-консультант компании eDiscom. "
    "Специализируешься на сетевом оборудовании (Cisco, Huawei, Fortinet, HPE Aruba и др.). "
    "Пишешь клиентам персональные сообщения-касания после их запросов. "
    "Стиль: дружелюбный, профессиональный, обращение на «вы», кратко. "
    "Отвечай только на русском языке."
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _msk_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).astimezone(dt.timezone(dt.timedelta(hours=3)))


def _is_quiet_hours() -> bool:
    h = _msk_now().hour
    return h >= 22 or h < 9


def _hours_since(ts_str: str) -> float:
    ts = dt.datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    return (dt.datetime.now(dt.timezone.utc) - ts).total_seconds() / 3600


def _recently_active(u: dict, hours: float = 24.0) -> bool:
    last_seen = u.get("last_seen")
    if not last_seen:
        return False
    return _hours_since(last_seen) < hours


def _get_history_text(user_id: int) -> str:
    msgs = chat_history.get_last_messages(user_id, 20)
    if not msgs:
        return ""
    lines = []
    for m in msgs:
        role = "Клиент" if m["direction"] == "in" else "Валли"
        lines.append(f"{role}: {m['text'][:200]}")
    return "\n".join(lines)


def _best_article(u: dict) -> str:
    arts = u.get("articles", [])
    if arts:
        return arts[-1]
    lrt = u.get("last_request_text", "")
    return lrt[:50] if lrt else ""


async def _generate(prompt: str) -> str:
    resp = await _client.messages.create(
        model="claude-opus-4-7",
        max_tokens=350,
        system=_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text.strip()


async def _send_touch(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    text: str,
    tp_key: str,
) -> None:
    try:
        await context.bot.send_message(chat_id=user_id, text=text)
        await users.add_touchpoint(user_id, tp_key)
        logger.info("Touchpoint %s → пользователь %s", tp_key, user_id)
    except Exception:
        logger.exception("Ошибка touchpoint %s для %s", tp_key, user_id)


# ---------------------------------------------------------------------------
# Message generators
# ---------------------------------------------------------------------------

async def _day1_message(u: dict) -> str:
    first_name = u.get("first_name") or "Клиент"
    article = _best_article(u)
    history = _get_history_text(u.get("user_id") or 0)
    prompt = (
        f"Напиши персональное сообщение-касание клиенту через 1 день после его запроса.\n"
        f"Имя клиента: {first_name}\n"
        f"Артикул/запрос: {article or '(не указан)'}\n"
        f"История диалога:\n{history or '(нет данных)'}\n\n"
        f"Базовый шаблон: «Добрый день, {first_name}! Как вам наше предложение по {article or 'вашему запросу'}? "
        f"Есть ли таргет по цене? Устраивают ли условия поставки и оплаты?»\n"
        f"Сделай сообщение персональным, опираясь на историю. Кратко, 2-4 предложения."
    )
    return await _generate(prompt)


async def _day3_message(u: dict) -> str:
    first_name = u.get("first_name") or "Клиент"
    article = _best_article(u)

    price_note = ""
    if article:
        snapshot = u.get("touchpoint_price_snapshot", {})
        old_price = snapshot.get("price") if snapshot.get("article") == article else None
        results = search(article)
        if results and not results[0].get("fuzzy") and not results[0].get("no_price"):
            current_price = results[0].get("price", "")
            if old_price and current_price and old_price != current_price:
                price_note = (
                    f"Цена на {article} изменилась: было {old_price}, стало {current_price}."
                )

    if price_note:
        prompt = (
            f"Напиши клиенту {first_name} короткое сообщение об изменении цены.\n"
            f"{price_note}\n"
            f"Упомяни изменение и уточни, актуален ли интерес. 2-3 предложения."
        )
    else:
        prompt = (
            f"Напиши персональное сообщение клиенту {first_name} через 3 дня после запроса "
            f"по {article or 'оборудованию'}.\n"
            f"Уточни — есть ли новости по запросу, актуален ли интерес. "
            f"1-2 предложения, дружелюбно."
        )
    return await _generate(prompt)


async def _week1_message(u: dict) -> str:
    first_name = u.get("first_name") or "Клиент"
    article = _best_article(u)
    history = _get_history_text(u.get("user_id") or 0)
    prompt = (
        f"Напиши клиенту {first_name} информационное сообщение через неделю после запроса "
        f"по {article or 'сетевому оборудованию'}.\n"
        f"История диалога:\n{history or '(нет данных)'}\n\n"
        f"Придумай актуальный факт или новость по теме запроса "
        f"(тренды, новые модели, EoL оборудование, советы по выбору). "
        f"Персонализируй под интересы клиента. Кратко, 2-4 предложения."
    )
    return await _generate(prompt)


def _week4_message(u: dict) -> str:
    first_name = u.get("first_name") or "Клиент"
    article = _best_article(u)
    subject = article if article else "вашему запросу"
    return (
        f"Добрый день, {first_name}! "
        f"Как продвигается проект/тендер по {subject}? "
        f"Есть ли предварительные результаты? "
        f"Готовы помочь с актуальной ценой или подбором альтернатив."
    )


async def _monday_message(u: dict, msk_now: dt.datetime) -> str:
    first_name = u.get("first_name") or "Клиент"
    articles = u.get("articles", [])
    month_names = {
        1: "январе", 2: "феврале", 3: "марте", 4: "апреле",
        5: "мае", 6: "июне", 7: "июле", 8: "августе",
        9: "сентябре", 10: "октябре", 11: "ноябре", 12: "декабре",
    }
    month_name = month_names.get(msk_now.month, str(msk_now.month))
    arts_str = ", ".join(articles[:5]) if articles else "сетевым оборудованием"
    prompt = (
        f"Напиши персональное приветственное сообщение для клиента {first_name} "
        f"в начале {month_name} {msk_now.year} года (первый рабочий понедельник месяца).\n"
        f"Клиент ранее интересовался: {arts_str}.\n"
        f"Придумай актуальный инфоповод: праздники или события этого месяца, "
        f"новости телеком-отрасли, анонсы EoL оборудования, "
        f"сезонные поводы для обновления инфраструктуры.\n"
        f"Персонализируй под интересы клиента. Кратко, 2-4 предложения. Дружелюбно."
    )
    return await _generate(prompt)


# ---------------------------------------------------------------------------
# Main job
# ---------------------------------------------------------------------------

async def run_touchpoints_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Hourly job: send personal touchpoints to clients after their requests."""
    if _is_quiet_hours():
        return

    supplier_ids = {str(s["chat_id"]) for s in supplier_requests.load_suppliers()}
    supplier_ids.add(str(MANAGER_CHAT_ID))

    all_users_data = await users.get_all_users()
    msk_now = _msk_now()

    is_first_monday = msk_now.weekday() == 0 and msk_now.day <= 7
    monday_key = f"monday_{msk_now.year}_{msk_now.month:02d}"

    for uid, u in all_users_data.items():
        if uid in supplier_ids:
            continue

        user_id = u.get("user_id") or int(uid)
        touchpoints = u.get("touchpoints", [])

        if _recently_active(u):
            continue

        last_request_time = u.get("last_request_time")

        if last_request_time:
            hours = _hours_since(last_request_time)

            if "day1" not in touchpoints and 20 <= hours <= 28:
                text = await _day1_message(u)
                # Save price snapshot for day3 comparison
                article = _best_article(u)
                if article:
                    results = search(article)
                    if results and not results[0].get("fuzzy") and not results[0].get("no_price"):
                        await users.save_price_snapshot(user_id, article, results[0].get("price", ""))
                await _send_touch(context, user_id, text, "day1")
                continue

            if "day3" not in touchpoints and 68 <= hours <= 76:
                # Reload user to get fresh price snapshot saved at day1
                fresh = (await users.get_all_users()).get(uid, u)
                text = await _day3_message(fresh)
                await _send_touch(context, user_id, text, "day3")
                continue

            if "week1" not in touchpoints and 144 <= hours <= 192:
                text = await _week1_message(u)
                await _send_touch(context, user_id, text, "week1")
                continue

            if "week4" not in touchpoints and 648 <= hours <= 744:
                text = _week4_message(u)
                await _send_touch(context, user_id, text, "week4")
                continue

        # Monday touchpoint — clients who ever made a request
        if is_first_monday and monday_key not in touchpoints and last_request_time:
            text = await _monday_message(u, msk_now)
            await _send_touch(context, user_id, text, monday_key)
