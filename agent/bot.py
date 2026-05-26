import os
import re
import signal
import logging
import datetime as dt
from anthropic import AsyncAnthropic
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, ContextTypes, filters

import users
import email_sender
from search import search, search_containing
import supplier_requests
import chat_history

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

TOKEN = os.environ["TG_BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
MANAGER_CHAT_ID = int(os.environ["MANAGER_CHAT_ID"])

anthropic_client = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

INACTIVITY_MINUTES = 30

SYSTEM_PROMPT = """Ты — Валли, робот-закупщик компании eDiscom. Специализируешься на сетевом оборудовании: Cisco, Huawei, Fortinet, HPE Aruba и других брендах.

Твой характер:
- Бойкий и дружелюбный, но профессиональный
- Обращаешься к клиентам на «вы», но без формальной холодности
- Любишь точные цифры и конкретику
- Иногда шутишь про «железо» и «порты», но в меру
- Отвечаешь исключительно на русском языке

Твои возможности:
- Ищешь цены в прайс-листе по артикулам оборудования
- Знаешь стандартные сроки поставки (4–5 недель)
- Если оборудование не нашлось в прайсе — уточняешь у поставщика
- Консультируешь по выбору оборудования в рамках своей экспертизы
- Когда клиент готов купить — запрашиваешь реквизиты компании для счёта

Цены из прайса показывает система автоматически. Твоя задача — помочь клиенту разобраться с выбором, ответить на вопросы и при необходимости предложить альтернативы.

В конце каждого ответа, где упоминается оборудование или цены, добавляй ссылку: www.ediscom.ru

Компания eDiscom — дистрибьютор сетевого оборудования. Все цены указаны в рублях с НДС.

Важно: отвечай только на русском языке, кратко и по делу."""

_ARTICLE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-_/\\.+\s]{1,60}$")

_PURCHASE_RE = re.compile(
    r"\b(да\b|согласен|согласна|оформляем|оформить|выставите?\s+счёт|выставьте\s+счёт|"
    r"готов\b|готова\b|берём|берем|покупаем|заказываем|заказать|"
    r"хочу\s+купить|буду\s+брать|договорились|по\s+рукам|давайте\s+оформим)\b",
    re.IGNORECASE,
)

_STALE_TRIGGER_RE = re.compile(
    r"уточни(ть|те)?\s+актуальность|актуальн(ую|ая)\s+цен",
    re.IGNORECASE,
)

MAX_HISTORY = 10


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _user_label(user) -> str:
    return f"@{user.username}" if user.username else user.first_name


def _looks_like_article(text: str) -> bool:
    if len(text) > 60:
        return False
    if re.search(r"[?!.,;]", text):
        return False
    return bool(_ARTICLE_RE.match(text)) and bool(re.search(r"\d", text) or re.search(r"[A-Za-z]{2,}", text))


def _is_purchase_intent(text: str) -> bool:
    return bool(_PURCHASE_RE.search(text))


def _format_result(item: dict) -> str:
    text = (
        f"Артикул: {item['article']}\n"
        f"Кондиция: {item['condition']}\n"
        f"Цена: {item['price']}\n"
        f"Срок поставки: 4–5 недель."
    )
    if item.get("stale"):
        text += "\n⚠️ Цена устаревшая — запрошу актуальную у поставщиков."
    return text


_OPTIONAL_QUESTIONS = (
    "Если уточните детали — подберу оптимальный вариант:\n"
    "— Тендер или запрос? Срок?\n"
    "— Таргет по цене?\n"
    "— Кондиция и комплектация?"
)

_SUPPLIER_REQUEST_TEXT = (
    "🔍 Price Request\n\n"
    "Part Number: {article}\n"
    "Condition: new\n"
    "Quantity: 1 pcs\n\n"
    "Please reply with:\n"
    "PRICE: [amount] USD/RMB\n"
    "LEAD TIME: [days/weeks]"
)


def _last_article(context: ContextTypes.DEFAULT_TYPE) -> str | None:
    arts = context.user_data.get("last_articles")
    if not arts:
        return None
    return arts[0].split(" — ")[0].strip()


async def _request_supplier_price(
    article: str,
    user,
    user_label: str,
    context: ContextTypes.DEFAULT_TYPE,
) -> int | None:
    """
    Create pending_selection request and ask manager to choose suppliers.
    Returns request_id, or None if a request for this article is already active.
    """
    if supplier_requests.get_existing_active(article, user.id):
        return None
    req = supplier_requests.create_request(article, user.id, user_label, status="pending_selection")
    suppliers = supplier_requests.load_suppliers()
    lines = [
        "🔍 Новый запрос цены",
        "",
        f"Артикул: {article}",
        f"Клиент: {user_label}",
        "",
        "Выберите поставщиков для запроса:",
        f"/ask_all {req['id']} — отправить всем",
    ]
    for s in suppliers:
        lines.append(f"/ask_{s['id']} {req['id']} — только {s['name']}")
    for i in range(len(suppliers)):
        for j in range(i + 1, len(suppliers)):
            si, sj = suppliers[i], suppliers[j]
            lines.append(f"/ask_{si['id']}_{sj['id']} {req['id']} — {si['name']} и {sj['name']}")
    try:
        await context.bot.send_message(chat_id=MANAGER_CHAT_ID, text="\n".join(lines))
    except Exception:
        logger.exception("Ошибка уведомления менеджера о выборе поставщика")
    return req["id"]


def _get_history(context: ContextTypes.DEFAULT_TYPE) -> list[dict]:
    if "history" not in context.user_data:
        context.user_data["history"] = []
    return context.user_data["history"]


def _add_to_history(context: ContextTypes.DEFAULT_TYPE, role: str, content: str) -> None:
    history = _get_history(context)
    history.append({"role": role, "content": content})
    if len(history) > MAX_HISTORY * 2:
        context.user_data["history"] = history[-(MAX_HISTORY * 2):]


async def _reply(update: Update, user, text: str, **kwargs) -> None:
    """Send reply to user and log it to chat history file."""
    await update.message.reply_text(text, **kwargs)
    chat_history.append_message(user.id, user.username, user.first_name, "out", text)


# ---------------------------------------------------------------------------
# Claude
# ---------------------------------------------------------------------------

async def _ask_claude(
    user_text: str,
    context: ContextTypes.DEFAULT_TYPE,
    extra_instruction: str | None = None,
) -> str:
    history = _get_history(context)
    messages = history + [{"role": "user", "content": user_text}]

    system: list[dict] = [
        {"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}
    ]
    first_name = context.user_data.get("user_first_name")
    if first_name:
        system.append({"type": "text", "text": f"Пользователя зовут {first_name}, обращайся к нему по имени в разговоре."})
    if extra_instruction:
        system.append({"type": "text", "text": extra_instruction})

    response = await anthropic_client.messages.create(
        model="claude-opus-4-7",
        max_tokens=1024,
        system=system,
        messages=messages,
    )
    reply = response.content[0].text
    _add_to_history(context, "user", user_text)
    _add_to_history(context, "assistant", reply)
    return reply


# ---------------------------------------------------------------------------
# Manager notifications
# ---------------------------------------------------------------------------

async def _notify_new_user(user, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_link = f"https://t.me/{user.username}" if user.username else f"tg://user?id={user.id}"
    label = _user_label(user)
    text = (
        f"🆕 Новый пользователь!\n\n"
        f"Имя: {user.first_name or '—'}\n"
        f"Username: {label}\n"
        f"ID: {user.id}"
    )
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Написать клиенту", url=user_link)]])
    try:
        await context.bot.send_message(chat_id=MANAGER_CHAT_ID, text=text, reply_markup=keyboard)
    except Exception:
        logger.exception("Ошибка уведомления менеджера о новом пользователе")


async def _send_manager_notification(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    user_label = _user_label(user)
    user_link = f"https://t.me/{user.username}" if user.username else f"tg://user?id={user.id}"

    articles = context.user_data.get("last_articles") or []
    requisites = context.user_data.get("requisites")

    lines = ["Новая заявка!", "", f"Клиент: {user_label}", f"ID: {user.id}"]
    if articles:
        lines += ["", "Интересовался:"] + [f"  {a}" for a in articles]
    else:
        lines += ["", "Товар: не указан (уточните у клиента)"]
    lines += ["", f"Реквизиты: {requisites}" if requisites else "Реквизиты: не предоставлены"]

    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Написать клиенту", url=user_link)]])

    try:
        await context.bot.send_message(
            chat_id=MANAGER_CHAT_ID,
            text="\n".join(lines),
            reply_markup=keyboard,
        )
        logger.info("Уведомление менеджеру отправлено для %s", user.id)
    except Exception:
        logger.exception("Ошибка отправки уведомления менеджеру")


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

async def _send_chat_email_now(user_label: str, context: ContextTypes.DEFAULT_TYPE) -> None:
    history = context.user_data.get("history", [])
    last_sent = context.user_data.get("email_sent_at_len", 0)
    if not history or len(history) <= last_sent:
        return
    context.user_data["email_sent_at_len"] = len(history)
    try:
        await email_sender.send_chat_history(user_label, list(history))
    except Exception:
        logger.exception("Ошибка отправки email для %s", user_label)


# ---------------------------------------------------------------------------
# Inactivity job
# ---------------------------------------------------------------------------

async def _inactivity_callback(context: ContextTypes.DEFAULT_TYPE) -> None:
    data = context.job.data
    user_data = data["user_data"]
    user_label = data["user_label"]
    user_id = data["user_id"]
    username = data.get("username")

    history = user_data.get("history", [])
    dialog_started_at = user_data.get("dialog_started_at")

    # Email (existing behaviour)
    last_sent = user_data.get("email_sent_at_len", 0)
    if history and len(history) > last_sent:
        user_data["email_sent_at_len"] = len(history)
        try:
            await email_sender.send_chat_history(user_label, list(history))
            logger.info("Email после неактивности отправлен: %s", user_label)
        except Exception:
            logger.exception("Ошибка email после неактивности для %s", user_label)

    # Dialog summary → manager
    if history and dialog_started_at:
        duration = dt.datetime.now() - dialog_started_at
        total_min = int(duration.total_seconds() // 60)
        total_sec = int(duration.total_seconds() % 60)
        duration_str = f"{total_min} мин {total_sec} сек"

        summary = "—"
        try:
            resp = await anthropic_client.messages.create(
                model="claude-opus-4-7",
                max_tokens=300,
                system="Ты помощник, кратко резюмируешь диалоги. Отвечай только на русском языке.",
                messages=list(history) + [{
                    "role": "user",
                    "content": (
                        "Кратко (2-3 предложения) резюмируй этот диалог: "
                        "о чём говорили, чем интересовался клиент, к чему пришли."
                    ),
                }],
            )
            summary = resp.content[0].text
        except Exception:
            logger.exception("Ошибка генерации резюме диалога для %s", user_label)

        user_link = f"https://t.me/{username}" if username else f"tg://user?id={user_id}"
        text = (
            f"📊 Диалог завершён\n\n"
            f"Пользователь: {user_label}\n"
            f"Длительность: {duration_str}\n"
            f"Сообщений: {len(history)}\n\n"
            f"📝 Резюме:\n{summary}"
        )
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Написать клиенту", url=user_link)]])
        try:
            await context.bot.send_message(
                chat_id=MANAGER_CHAT_ID, text=text, reply_markup=keyboard
            )
        except Exception:
            logger.exception("Ошибка отправки резюме диалога менеджеру для %s", user_label)

    user_data.pop("dialog_started_at", None)


def _schedule_inactivity_job(user, user_label: str, context: ContextTypes.DEFAULT_TYPE) -> None:
    job_name = f"inactivity_{user.id}"
    for job in context.job_queue.get_jobs_by_name(job_name):
        job.schedule_removal()
    context.job_queue.run_once(
        _inactivity_callback,
        when=INACTIVITY_MINUTES * 60,
        data={
            "user_data": context.user_data,
            "user_label": user_label,
            "user_id": user.id,
            "username": user.username,
            "first_name": user.first_name,
        },
        name=job_name,
    )


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    is_new = await users.update_user(user)
    context.user_data.clear()
    context.user_data["user_first_name"] = user.first_name
    context.user_data["dialog_started_at"] = dt.datetime.now()
    user_label = _user_label(user)
    for job in context.job_queue.get_jobs_by_name(f"inactivity_{user.id}"):
        job.schedule_removal()
    chat_history.append_message(user.id, user.username, user.first_name, "in", "/start")
    if is_new:
        await _notify_new_user(user, context)
    reply = await _ask_claude("Поздоровайся и кратко расскажи, чем можешь помочь.", context)
    await _reply(update, user, reply)
    _schedule_inactivity_job(user, user_label, context)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.message.text.strip()
    if not query:
        return

    user = update.effective_user
    logger.info("Запрос от %s (%s): %s", user.id, user.username, query)
    is_new = await users.update_user(user)
    context.user_data["user_first_name"] = user.first_name
    user_label = _user_label(user)

    # --- Сообщение от поставщика ---
    supplier = supplier_requests.get_supplier_by_chat_id(user.id)
    if supplier:
        sent_reqs = supplier_requests.get_sent_to_suppliers()
        lines = [f"📨 Reply from {supplier['name']}:", "", query]
        if sent_reqs:
            lines += ["", "Клиент ожидает ответа", "", "Открытые заявки:"]
            for r in sent_reqs:
                lines.append(
                    f"  /price {r['id']} <цена> <срок>  —  {r['article']}  ({r['client_label']})"
                )
        try:
            await context.bot.send_message(chat_id=MANAGER_CHAT_ID, text="\n".join(lines))
        except Exception:
            logger.exception("Ошибка пересылки ответа поставщика менеджеру")
        await update.message.reply_text("Thanks! 👍")
        return

    # --- Логирование и трекинг нового пользователя ---
    chat_history.append_message(user.id, user.username, user.first_name, "in", query)
    if is_new:
        await _notify_new_user(user, context)
    if not context.user_data.get("dialog_started_at"):
        context.user_data["dialog_started_at"] = dt.datetime.now()

    # --- Ожидаем реквизиты ---
    if context.user_data.get("awaiting_requisites"):
        context.user_data["requisites"] = query
        context.user_data["awaiting_requisites"] = False
        await _send_manager_notification(update, context)
        reply = "Передал вашу заявку менеджеру — свяжутся в течение рабочего дня."
        _add_to_history(context, "user", query)
        _add_to_history(context, "assistant", reply)
        await _reply(update, user, reply)
        await _send_chat_email_now(user_label, context)
        _schedule_inactivity_job(user, user_label, context)
        return

    # --- Клиент уточняет детали (необязательно) ---
    valli_state = context.user_data.get("valli_state")
    if valli_state and valli_state.get("mode") == "awaiting_details":
        if not _looks_like_article(query) and not _is_purchase_intent(query):
            article = valli_state.get("article", "")
            context.user_data.pop("valli_state", None)
            if re.search(_STALE_TRIGGER_RE, query) and article:
                req_id = await _request_supplier_price(article, user, user_label, context)
                reply = (
                    f"Направил запрос менеджеру по артикулу {article} — уточняю актуальную цену, отвечу в течение рабочего дня."
                    if req_id else
                    f"Запрос по артикулу {article} уже в обработке — ожидаем ответа."
                )
                _add_to_history(context, "user", query)
                _add_to_history(context, "assistant", reply)
                await _reply(update, user, reply)
                _schedule_inactivity_job(user, user_label, context)
                return
            reply = await _ask_claude(
                query,
                context,
                extra_instruction=(
                    f"Клиент только что получил цену на артикул {article} "
                    "и уточняет детали запроса. Учти эту информацию в ответе."
                ),
            )
            await _reply(update, user, reply)
            _schedule_inactivity_job(user, user_label, context)
            return
        context.user_data.pop("valli_state", None)
        # Fall through to purchase intent check or article search

    # --- Готовность к покупке ---
    if _is_purchase_intent(query):
        context.user_data.pop("valli_state", None)
        if not context.user_data.get("requisites"):
            reply = await _ask_claude(
                query, context,
                extra_instruction=(
                    "Клиент выразил готовность к покупке. "
                    "Вежливо попроси реквизиты компании: название, ИНН и контактное лицо — "
                    "они нужны для выставления счёта. Коротко, по-деловому, в своём стиле."
                ),
            )
            context.user_data["awaiting_requisites"] = True
        else:
            await _send_manager_notification(update, context)
            reply = "Передал вашу заявку менеджеру — свяжутся в течение рабочего дня."
            _add_to_history(context, "user", query)
            _add_to_history(context, "assistant", reply)
            await _reply(update, user, reply)
            await _send_chat_email_now(user_label, context)
            _schedule_inactivity_job(user, user_label, context)
            return
        await _reply(update, user, reply)
        _schedule_inactivity_job(user, user_label, context)
        return

    # --- Клиент выбирает артикул из списка ---
    valli_state = context.user_data.get("valli_state")
    if valli_state and valli_state.get("mode") == "disambiguation":
        candidates = valli_state.get("candidates", [])
        found = search(query)
        exact = [r for r in found if not r.get("fuzzy") and not r.get("no_price")]
        if exact:
            selected = exact[0]
            price_text = _format_result(selected)
            context.user_data["last_articles"] = [f"{selected['article']} — {selected['price']}"]
            _add_to_history(context, "user", query)
            _add_to_history(context, "assistant", price_text)
            await _reply(update, user, price_text)
            if selected.get("stale"):
                req_id = await _request_supplier_price(selected["article"], user, user_label, context)
                if req_id:
                    stale_msg = "Направил запрос менеджеру — уточняю актуальную цену, отвечу в течение рабочего дня."
                    await _reply(update, user, stale_msg)
            context.user_data["valli_state"] = {"mode": "awaiting_details", "article": selected["article"]}
        else:
            lines = [f"— {c['article']} ({c['condition']})" for c in candidates]
            reply = "Не смог распознать выбор. Введите артикул из списка:\n\n" + "\n".join(lines)
            _add_to_history(context, "user", query)
            _add_to_history(context, "assistant", reply)
            await _reply(update, user, reply)
        _schedule_inactivity_job(user, user_label, context)
        return

    # --- Поиск по артикулу ---
    if _looks_like_article(query):
        # Always check substring matches first — even if exact match exists
        candidates = search_containing(query)

        if len(candidates) > 1:
            # Multiple variants found — show disambiguation list
            lines = [f"— {c['article']} ({c['condition']})" for c in candidates]
            q_display = query.upper()
            reply = (
                f"По запросу {q_display} найдено несколько вариантов, "
                f"уточните какой именно:\n\n"
                + "\n".join(lines)
            )
            context.user_data["valli_state"] = {
                "mode": "disambiguation",
                "candidates": candidates,
                "query": query,
            }
            _add_to_history(context, "user", query)
            _add_to_history(context, "assistant", reply)
            await _reply(update, user, reply)
            _schedule_inactivity_job(user, user_label, context)
            return

        if len(candidates) == 1:
            # Single match — show price directly
            precise = search(candidates[0]["article"])
            item = (
                precise[0]
                if precise and not precise[0].get("fuzzy") and not precise[0].get("no_price")
                else None
            )
            if item:
                price_text = _format_result(item)
                context.user_data["last_articles"] = [f"{item['article']} — {item['price']}"]
                _add_to_history(context, "user", query)
                _add_to_history(context, "assistant", price_text)
                await _reply(update, user, price_text)
                if item.get("stale"):
                    req_id = await _request_supplier_price(item["article"], user, user_label, context)
                    if req_id:
                        stale_msg = "Направил запрос менеджеру — уточняю актуальную цену, отвечу в течение рабочего дня."
                        await _reply(update, user, stale_msg)
                context.user_data["valli_state"] = {"mode": "awaiting_details", "article": item["article"]}
            else:
                reply = await _ask_claude(query, context)
                await _reply(update, user, reply)
            _schedule_inactivity_job(user, user_label, context)
            return

        # No substring match — check for exact no_price or fuzzy results
        results = search(query)
        if results:
            first = results[0]

            if first.get("no_price"):
                article = first["article"]
                reply = (
                    f"Цена по артикулу {article} ранее не запрашивалась — "
                    f"уточню у поставщика и отвечу чуть позже."
                )
                _add_to_history(context, "user", query)
                _add_to_history(context, "assistant", reply)
                await _reply(update, user, reply)
                try:
                    await context.bot.send_message(
                        chat_id=MANAGER_CHAT_ID,
                        text=(
                            f"⚠️ Запрос цены\n\n"
                            f"Артикул: {article}\n"
                            f"Клиент: {user_label} (ID: {user.id})\n\n"
                            f"Цена в прайсе отсутствует — нужно уточнить у поставщика."
                        ),
                    )
                except Exception:
                    logger.exception("Ошибка отправки уведомления менеджеру (no_price)")
                _schedule_inactivity_job(user, user_label, context)
                return

            # Fuzzy fallback — show with prices
            parts = [_format_result(item) for item in results]
            body = "\n\n".join(parts)
            reply = "Точного совпадения не нашёл. Возможно, вы имели в виду:\n\n" + body
            context.user_data["last_articles"] = [f"{r['article']} — {r['price']}" for r in results]
            _add_to_history(context, "user", query)
            _add_to_history(context, "assistant", reply)
            await _reply(update, user, reply)
            context.user_data["valli_state"] = {"mode": "awaiting_details", "article": first["article"]}
            _schedule_inactivity_job(user, user_label, context)
            return

        reply = await _ask_claude(
            query, context,
            extra_instruction=(
                f"Клиент спросил артикул «{query}», но его нет в нашем прайсе. "
                "Скажи, что уточнишь у поставщика и вернёшься с ответом. "
                "Кратко, дружелюбно, в своём стиле."
            ),
        )
    else:
        if re.search(_STALE_TRIGGER_RE, query):
            article = _last_article(context)
            if article:
                req_id = await _request_supplier_price(article, user, user_label, context)
                reply = (
                    f"Направил запрос менеджеру по артикулу {article} — уточняю актуальную цену, отвечу в течение рабочего дня."
                    if req_id else
                    f"Запрос по артикулу {article} уже в обработке — ожидаем ответа."
                )
                _add_to_history(context, "user", query)
                _add_to_history(context, "assistant", reply)
                await _reply(update, user, reply)
                _schedule_inactivity_job(user, user_label, context)
                return
        reply = await _ask_claude(query, context)

    await _reply(update, user, reply)
    _schedule_inactivity_job(user, user_label, context)


# ---------------------------------------------------------------------------
# Manager commands
# ---------------------------------------------------------------------------

async def cmd_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != MANAGER_CHAT_ID:
        return
    args = context.args or []
    if len(args) < 3:
        await update.message.reply_text("Формат: /price <id> <цена> <срок>\nПример: /price 5 140000 3-4 недели")
        return
    try:
        request_id = int(args[0])
    except ValueError:
        await update.message.reply_text("ID должен быть числом")
        return
    price = args[1]
    lead_time = " ".join(args[2:])
    req = supplier_requests.get_request(request_id)
    if not req:
        await update.message.reply_text(f"Заявка #{request_id} не найдена")
        return
    if req["status"] == "closed":
        await update.message.reply_text(f"Заявка #{request_id} уже закрыта")
        return
    reply_to_client = (
        f"✅ Актуальная цена на {req['article']}:\n"
        f"Цена: {price} руб. с НДС\n"
        f"Срок поставки: {lead_time}"
    )
    try:
        await context.bot.send_message(chat_id=req["client_chat_id"], text=reply_to_client)
        supplier_requests.close_request(request_id)
        await update.message.reply_text(f"✅ Ответ отправлен клиенту {req['client_label']}")
    except Exception:
        logger.exception("Ошибка отправки ответа клиенту по заявке %s", request_id)
        await update.message.reply_text("Ошибка при отправке клиенту")


_STATUS_LABELS = {
    "pending_selection": "⏳ ждёт выбора",
    "queued":            "🕘 в очереди 09:00",
    "pending":           "📤 у поставщиков",
    "closed":            "✅ закрыта",
}


async def cmd_suppliers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != MANAGER_CHAT_ID:
        return
    suppliers = supplier_requests.load_suppliers()
    active = supplier_requests.get_all_active()
    lines = ["📋 Поставщики:", ""]
    for s in suppliers:
        lines.append(f"• {s['name']}  (ID: {s['id']},  chat_id: {s['chat_id']})")
    lines += ["", f"Активных заявок: {len(active)}"]
    if active:
        lines.append("")
        for r in active[-10:]:
            status_label = _STATUS_LABELS.get(r["status"], r["status"])
            lines.append(
                f"  #{r['id']}  {r['article']}  →  {r['client_label']}  "
                f"({r['created_at'][:10]})  {status_label}"
            )
    await update.message.reply_text("\n".join(lines))


async def cmd_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != MANAGER_CHAT_ID:
        return
    args = context.args or []
    if not args:
        await update.message.reply_text("Формат: /chat <user_id>")
        return
    try:
        target_id = int(args[0])
    except ValueError:
        await update.message.reply_text("ID должен быть числом")
        return
    messages = chat_history.get_last_messages(target_id, 20)
    if not messages:
        await update.message.reply_text(f"Чат с пользователем {target_id} не найден")
        return
    lines = [f"💬 Последние {len(messages)} сообщений (ID {target_id}):", ""]
    for m in messages:
        arrow = "→" if m["direction"] == "in" else "←"
        lines.append(f"[{m['date']} {m['time']}] {arrow} {m['text'][:200]}")
    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "\n...(обрезано)"
    await update.message.reply_text(text)


# ---------------------------------------------------------------------------
# Manager: /ask_* supplier selection
# ---------------------------------------------------------------------------

async def handle_ask_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != MANAGER_CHAT_ID:
        return
    parts = update.message.text.strip().split()
    if len(parts) < 2:
        await update.message.reply_text("Формат: /ask_all <id>  или  /ask_1_2 <id>")
        return
    command = parts[0].lstrip("/")  # e.g. "ask_all" or "ask_1_2"
    try:
        request_id = int(parts[1])
    except ValueError:
        await update.message.reply_text("Неверный ID заявки")
        return

    req = supplier_requests.get_request(request_id)
    if not req:
        await update.message.reply_text(f"Заявка #{request_id} не найдена")
        return
    if req["status"] != "pending_selection":
        await update.message.reply_text(
            f"Заявка #{request_id} уже обработана (статус: {req['status']})"
        )
        return

    all_suppliers = supplier_requests.load_suppliers()
    supplier_map = {s["id"]: s for s in all_suppliers}

    if command == "ask_all":
        selected_ids = [s["id"] for s in all_suppliers]
    else:
        id_parts = command[4:].split("_")  # strip "ask_", split remaining
        try:
            selected_ids = [int(x) for x in id_parts]
        except ValueError:
            await update.message.reply_text("Неверный формат команды")
            return

    selected_suppliers = [supplier_map[sid] for sid in selected_ids if sid in supplier_map]
    if not selected_suppliers:
        await update.message.reply_text("Поставщики не найдены")
        return

    working = supplier_requests.is_working_hours()
    supplier_requests.set_selected_suppliers(request_id, selected_ids, queued=not working)

    if working:
        text = _SUPPLIER_REQUEST_TEXT.format(article=req["article"])
        for s in selected_suppliers:
            try:
                await context.bot.send_message(chat_id=s["chat_id"], text=text)
            except Exception:
                logger.exception("Ошибка отправки запроса поставщику %s", s["name"])
        names = ", ".join(s["name"] for s in selected_suppliers)
        await update.message.reply_text(f"✅ Запрос отправлен: {names}")
        client_msg = (
            f"Отправил запрос поставщикам по артикулу {req['article']} — "
            f"отвечу в течение рабочего дня."
        )
    else:
        names = ", ".join(s["name"] for s in selected_suppliers)
        await update.message.reply_text(f"⏰ Запрос отправлю в 09:00 МСК: {names}")
        client_msg = (
            f"Запрос поставщикам по артикулу {req['article']} отправлю в 09:00 МСК — "
            f"ответ ожидается в течение рабочего дня."
        )
    try:
        await context.bot.send_message(chat_id=req["client_chat_id"], text=client_msg)
    except Exception:
        logger.exception("Ошибка уведомления клиента после выбора поставщика")


# ---------------------------------------------------------------------------
# Scheduled jobs
# ---------------------------------------------------------------------------

async def _send_queued_requests(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Daily job at 09:00 MSK: send overnight queued supplier requests."""
    queued = supplier_requests.get_queued()
    if not queued:
        return
    logger.info("09:00 МСК: отправка %d накопившихся запросов поставщикам", len(queued))
    all_suppliers = supplier_requests.load_suppliers()
    supplier_map = {s["id"]: s for s in all_suppliers}
    for req in queued:
        selected_ids = req.get("selected_supplier_ids") or [s["id"] for s in all_suppliers]
        text = _SUPPLIER_REQUEST_TEXT.format(article=req["article"])
        for sid in selected_ids:
            if sid in supplier_map:
                try:
                    await context.bot.send_message(chat_id=supplier_map[sid]["chat_id"], text=text)
                except Exception:
                    logger.exception("Ошибка отправки запроса поставщику %s", supplier_map[sid]["name"])
        supplier_requests.mark_pending(req["id"])
        try:
            await context.bot.send_message(
                chat_id=req["client_chat_id"],
                text=(
                    f"Отправил запрос поставщикам по артикулу {req['article']} — "
                    f"ожидайте ответа в течение рабочего дня."
                ),
            )
        except Exception:
            logger.exception("Ошибка уведомления клиента %s при утренней отправке", req["client_chat_id"])


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _on_reload_signal(signum, frame):
    logger.info("SIGUSR1: перезагрузка прайса")
    search.reload()


def main() -> None:
    signal.signal(signal.SIGUSR1, _on_reload_signal)
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("price", cmd_price))
    app.add_handler(CommandHandler("suppliers", cmd_suppliers))
    app.add_handler(CommandHandler("chat", cmd_chat))
    app.add_handler(MessageHandler(filters.Regex(r"^/ask_"), handle_ask_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    # 09:00 MSK = 06:00 UTC
    app.job_queue.run_daily(
        _send_queued_requests,
        time=dt.time(6, 0, 0, tzinfo=dt.timezone.utc),
    )
    logger.info("Бот Валли запущен")
    app.run_polling()


if __name__ == "__main__":
    main()
