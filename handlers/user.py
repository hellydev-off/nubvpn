import logging
import os
from datetime import datetime, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from db import create_request, get_request, get_user, mark_rules_seen
from marzban import MarzbanClient

logger = logging.getLogger(__name__)

ADMIN_IDS: frozenset[int] = frozenset(
    int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()
)

_STATUS_EMOJI = {
    "active": "🟢",
    "disabled": "🔴",
    "limited": "🟡",
    "expired": "⏰",
    "on_hold": "⏸",
}

_NO_ACCESS = "❌ У вас нет доступа. Обратитесь к администратору."

_RULES_TEXT = (
    "📋 *Важно — прочитай перед использованием*\n\n"
    "🔒 *1 ссылка = 1 человек*\n"
    "Твоя ссылка привязана лично к тебе. Если ты передашь её кому-то ещё — "
    "скорость и стабильность упадут у вас обоих. Не делай этого.\n\n"
    "👥 *Хочешь подключить кого-то ещё?*\n"
    "Напиши напрямую: @Hellylo — и я выдам отдельную ссылку.\n\n"
    "📈 *Больше людей = лучше серверы*\n"
    "Каждый новый пользователь помогает улучшать инфраструктуру для всех."
)


def _fmt_bytes(b: int | None) -> str:
    if not b:
        return "0 GB"
    return f"{b / 1024 ** 3:.2f} GB"


def _fmt_limit(b: int | None) -> str:
    return "Без лимита" if not b else _fmt_bytes(b)


def _fmt_expire(ts: int | None) -> str:
    if ts is None:
        return "Бессрочно"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def _status_emoji(status: str) -> str:
    return _STATUS_EMOJI.get(status, "❓")


def _welcome_text(note: str | None) -> str:
    greeting = f"👋 Привет, *{note}*!" if note else "👋 Привет!"
    return (
        f"{greeting}\n\n"
        "Доступные команды:\n"
        "/sub — Получить VLESS конфиг\n"
        "/info — Информация об аккаунте"
    )


def admin_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📩 Заявки",       callback_data="admin_requests"),
            InlineKeyboardButton("👥 Пользователи", callback_data="admin_listusers:0"),
        ],
        [
            InlineKeyboardButton("📊 Статистика",   callback_data="admin_stats"),
            InlineKeyboardButton("🟢 Онлайн",       callback_data="admin_online"),
        ],
        [
            InlineKeyboardButton("🔑 Моя ссылка",   callback_data="admin_mylink"),
            InlineKeyboardButton("📢 Рассылка",     callback_data="admin_broadcast"),
        ],
    ])


# ── /start ────────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id

    # Admin check FIRST — regardless of whether they're in the users table
    if user_id in ADMIN_IDS:
        await update.message.reply_text(
            "👋 *Панель администратора*\n\nВыберите действие:",
            parse_mode="Markdown",
            reply_markup=admin_menu_keyboard(),
        )
        return

    db_user = await get_user(user_id)

    if not db_user:
        req = await get_request(user_id)
        if req and req["status"] == "pending":
            await update.message.reply_text(
                "⏳ Ваша заявка уже отправлена и ожидает рассмотрения.\n"
                "Мы уведомим вас о решении."
            )
        elif req and req["status"] == "rejected":
            await update.message.reply_text(
                "❌ Ваша заявка была отклонена.\n"
                "Если вы считаете это ошибкой — напишите @Hellylo."
            )
        else:
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("📩 Подать заявку", callback_data="send_request")
            ]])
            await update.message.reply_text(
                "🔒 У вас нет доступа к боту.\n\n"
                "Хотите получить доступ? Нажмите кнопку ниже — "
                "администратор получит вашу заявку и рассмотрит её.",
                reply_markup=keyboard,
            )
        return

    if not db_user.get("seen_rules"):
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Я понял", callback_data="rules_accepted")
        ]])
        await update.message.reply_text(
            _RULES_TEXT,
            parse_mode="Markdown",
            reply_markup=keyboard,
        )
        return

    await update.message.reply_text(
        _welcome_text(db_user.get("note")), parse_mode="Markdown"
    )


# ── rules callback ────────────────────────────────────────────────────────────

async def handle_rules_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer("Отлично! Добро пожаловать 🎉")

    user_id = query.from_user.id
    await mark_rules_seen(user_id)

    db_user = await get_user(user_id)
    await query.edit_message_text(
        _welcome_text(db_user.get("note") if db_user else None),
        parse_mode="Markdown",
    )


# ── request submit callback ───────────────────────────────────────────────────

async def handle_request_submit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    user = query.from_user
    created = await create_request(
        telegram_id=user.id,
        tg_username=user.username,
        full_name=user.full_name,
    )

    if not created:
        await query.edit_message_text(
            "⏳ Ваша заявка уже отправлена и ожидает рассмотрения."
        )
        return

    await query.edit_message_text(
        "✅ Заявка отправлена!\n\n"
        "Администратор получит уведомление и рассмотрит вашу заявку. "
        "Мы сообщим вам о решении прямо в этот чат."
    )

    username_display = f"@{user.username}" if user.username else "нет username"
    admin_text = (
        f"📩 *Новая заявка на доступ*\n\n"
        f"👤 Имя: {user.full_name}\n"
        f"🔗 Username: {username_display}\n"
        f"🆔 ID: `{user.id}`"
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Принять", callback_data=f"req_accept:{user.id}"),
        InlineKeyboardButton("❌ Отклонить", callback_data=f"req_reject:{user.id}"),
    ]])
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=admin_text,
                parse_mode="Markdown",
                reply_markup=keyboard,
            )
        except Exception as exc:
            logger.warning("Failed to notify admin %d: %s", admin_id, exc)


# ── /sub ──────────────────────────────────────────────────────────────────────

async def cmd_sub(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    db_user = await get_user(user_id)
    if not db_user:
        await update.message.reply_text(_NO_ACCESS)
        return

    client: MarzbanClient = context.bot_data["marzban"]
    try:
        mu = await client.get_user(db_user["marzban_username"])
        link = client.vless_link(mu)
        await update.message.reply_text(
            f"Ваш VLESS конфиг:\n`{link}`\n\n"
            "_Скопируйте и вставьте в ваш VPN-клиент "
            "(v2rayNG, Hiddify, Streisand и др.)_",
            parse_mode="Markdown",
        )
    except Exception as exc:
        logger.exception("Error in /sub for TG %d: %s", user_id, exc)
        await update.message.reply_text(f"❌ Ошибка при получении конфига: {exc}")


# ── /info ─────────────────────────────────────────────────────────────────────

async def cmd_info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    db_user = await get_user(user_id)
    if not db_user:
        await update.message.reply_text(_NO_ACCESS)
        return

    client: MarzbanClient = context.bot_data["marzban"]
    try:
        mu = await client.get_user(db_user["marzban_username"])
        status = mu.get("status", "unknown")
        text = (
            f"{_status_emoji(status)} *Статус:* {status}\n"
            f"📊 *Трафик:* {_fmt_bytes(mu.get('used_traffic'))} / {_fmt_limit(mu.get('data_limit'))}\n"
            f"📅 *Истекает:* {_fmt_expire(mu.get('expire'))}"
        )
        await update.message.reply_text(text, parse_mode="Markdown")
    except Exception as exc:
        logger.exception("Error in /info for TG %d: %s", user_id, exc)
        await update.message.reply_text(f"❌ Ошибка при получении данных аккаунта: {exc}")
