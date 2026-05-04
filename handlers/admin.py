import logging
import math
import os
import re
from datetime import datetime, timezone

import httpx

_USERNAME_RE = re.compile(r"^[a-zA-Z0-9_-]{3,32}$")
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from db import (
    add_user, count_users, get_all_users, get_pending_requests,
    get_request, get_user, get_users_page, remove_user, update_request_status,
)
from marzban import MarzbanClient

logger = logging.getLogger(__name__)

ADMIN_IDS: frozenset[int] = frozenset(
    int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()
)
PAGE_SIZE = 10

_STATUS_EMOJI = {
    "active": "🟢",
    "disabled": "🔴",
    "limited": "🟡",
    "expired": "⏰",
    "on_hold": "⏸",
}


# ── helpers ──────────────────────────────────────────────────────────────────

def _is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def _fmt_bytes(b: int | None) -> str:
    if not b:
        return "0 GB"
    return f"{b / 1024 ** 3:.2f} GB"


def _fmt_limit(b: int | None) -> str:
    return "Unlimited" if not b else _fmt_bytes(b)


def _fmt_expire(ts: int | None) -> str:
    if ts is None:
        return "Never"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def _status_emoji(status: str) -> str:
    return _STATUS_EMOJI.get(status, "❓")


# ── /adduser ──────────────────────────────────────────────────────────────────

async def cmd_adduser(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    caller_id = update.effective_user.id
    if not _is_admin(caller_id):
        await update.message.reply_text("⛔ Нет доступа.")
        return

    args = context.args
    if not args:
        await update.message.reply_text(
            "Использование: `/adduser <telegram\\_id> [заметка]`",
            parse_mode="Markdown",
        )
        return

    try:
        target_id = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ telegram\\_id должен быть числом.", parse_mode="Markdown")
        return

    note = " ".join(args[1:]) if len(args) > 1 else ""

    # derive marzban username from telegram username (requests table) or fallback
    req = await get_request(target_id)
    raw_username = (req or {}).get("tg_username") or f"id{target_id}"
    marzban_username = re.sub(r"[^a-zA-Z0-9_-]", "_", raw_username)[:32]
    if len(marzban_username) < 3:
        marzban_username = f"id{target_id}"

    existing = await get_user(target_id)
    if existing:
        await update.message.reply_text(
            f"⚠️ Пользователь `{target_id}` уже зарегистрирован как `{existing['marzban_username']}`.",
            parse_mode="Markdown",
        )
        return

    client: MarzbanClient = context.bot_data["marzban"]
    try:
        try:
            marzban_user = await client.get_user(marzban_username)
            logger.info("Admin %d: Marzban user %s already exists", caller_id, marzban_username)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                marzban_user = await client.create_user(marzban_username)
                logger.info("Admin %d: created Marzban user %s", caller_id, marzban_username)
            else:
                raise

        link = client.vless_link(marzban_user)
        await add_user(target_id, marzban_username, caller_id, note)

        tg_name = f"@{req['tg_username']}" if req and req.get("tg_username") else str(target_id)
        text = (
            f"✅ *Пользователь добавлен\\!*\n"
            f"Telegram: {tg_name}\n"
            f"ID: `{target_id}`\n"
            f"Marzban: `{marzban_username}`\n"
            f"Заметка: {note or '—'}\n\n"
            f"VLESS конфиг:\n`{link}`"
        )
        await update.message.reply_text(text, parse_mode="MarkdownV2")
        logger.info("Admin %d added TG %d → Marzban %s", caller_id, target_id, marzban_username)
    except Exception as exc:
        logger.exception("Error in /adduser: %s", exc)
        await update.message.reply_text(f"❌ Ошибка: {exc}")


# ── /removeuser ───────────────────────────────────────────────────────────────

async def cmd_removeuser(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    caller_id = update.effective_user.id
    if not _is_admin(caller_id):
        await update.message.reply_text("⛔ Нет доступа.")
        return

    args = context.args
    if not args:
        await update.message.reply_text(
            "Использование: `/removeuser <telegram_id>`", parse_mode="Markdown"
        )
        return

    try:
        target_id = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ telegram_id должен быть числом.")
        return

    db_user = await get_user(target_id)
    if not db_user:
        await update.message.reply_text(
            f"❌ Пользователь `{target_id}` не зарегистрирован.", parse_mode="Markdown"
        )
        return

    marzban_username = db_user["marzban_username"]
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "✅ Подтвердить удаление", callback_data=f"confirm_remove:{target_id}"
            ),
            InlineKeyboardButton(
                "❌ Отмена", callback_data=f"cancel_remove:{target_id}"
            ),
        ]
    ])
    await update.message.reply_text(
        f"⚠️ Удалить пользователя `{target_id}` (`{marzban_username}`) из Marzban и базы данных?",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )


async def handle_remove_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    caller_id = query.from_user.id
    if not _is_admin(caller_id):
        await query.edit_message_text("⛔ Нет доступа.")
        return

    action, raw_id = query.data.split(":")
    target_id = int(raw_id)

    if action == "cancel_remove":
        await query.edit_message_text("❌ Удаление отменено.")
        return

    db_user = await get_user(target_id)
    if not db_user:
        await query.edit_message_text("⚠️ Пользователь уже был удалён.")
        return

    marzban_username = db_user["marzban_username"]
    client: MarzbanClient = context.bot_data["marzban"]
    try:
        await client.delete_user(marzban_username)
        await remove_user(target_id)
        await query.edit_message_text(
            f"🗑 Пользователь `{target_id}` (`{marzban_username}`) удалён из Marzban и базы данных.",
            parse_mode="Markdown",
        )
        logger.info(
            "Admin %d deleted TG %d → Marzban %s", caller_id, target_id, marzban_username
        )
    except Exception as exc:
        logger.exception("Error deleting user %d: %s", target_id, exc)
        await query.edit_message_text(f"❌ Ошибка: {exc}")


# ── /userinfo ─────────────────────────────────────────────────────────────────

async def cmd_userinfo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    caller_id = update.effective_user.id
    if not _is_admin(caller_id):
        await update.message.reply_text("⛔ Нет доступа.")
        return

    args = context.args
    if not args:
        await update.message.reply_text(
            "Использование: `/userinfo <telegram_id>`", parse_mode="Markdown"
        )
        return

    try:
        target_id = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ telegram_id должен быть числом.")
        return

    db_user = await get_user(target_id)
    if not db_user:
        await update.message.reply_text(
            f"❌ Пользователь `{target_id}` не зарегистрирован.", parse_mode="Markdown"
        )
        return

    client: MarzbanClient = context.bot_data["marzban"]
    try:
        mu = await client.get_user(db_user["marzban_username"])
        status = mu.get("status", "unknown")
        text = (
            f"*Информация о пользователе*\n"
            f"Telegram ID: `{target_id}`\n"
            f"Marzban: `{mu.get('username')}`\n"
            f"Заметка: {db_user.get('note') or '—'}\n"
            f"Статус: {_status_emoji(status)} {status}\n"
            f"Трафик: {_fmt_bytes(mu.get('used_traffic'))} / {_fmt_limit(mu.get('data_limit'))}\n"
            f"Истекает: {_fmt_expire(mu.get('expire'))}\n"
            f"Добавил: `{db_user.get('added_by')}`\n"
            f"Дата добавления: {(db_user.get('added_at') or '')[:10]}"
        )
        await update.message.reply_text(text, parse_mode="Markdown")
    except Exception as exc:
        logger.exception("Error in /userinfo: %s", exc)
        await update.message.reply_text(f"❌ Ошибка: {exc}")


# ── /listusers ────────────────────────────────────────────────────────────────

def _build_list_page(
    users: list[dict], page: int, total: int
) -> tuple[str, InlineKeyboardMarkup]:
    total_pages = max(1, math.ceil(total / PAGE_SIZE))
    start = page * PAGE_SIZE + 1
    end = min(start + PAGE_SIZE - 1, total)

    lines = [f"<b>👥 Пользователи ({start}–{end} из {total})</b>\n"]
    for u in users:
        note = _h(u.get("note") or "—")
        lines.append(
            f"<code>{u['telegram_id']}</code> | "
            f"<code>{_h(u['marzban_username'])}</code> | {note}"
        )
    lines.append(f"\nСтраница {page + 1}/{total_pages}")

    buttons: list[InlineKeyboardButton] = []
    if page > 0:
        buttons.append(
            InlineKeyboardButton("◀ Назад", callback_data=f"listusers:page:{page - 1}")
        )
    if page + 1 < total_pages:
        buttons.append(
            InlineKeyboardButton("Вперёд ▶", callback_data=f"listusers:page:{page + 1}")
        )

    keyboard = InlineKeyboardMarkup([buttons]) if buttons else InlineKeyboardMarkup([])
    return "\n".join(lines), keyboard


async def cmd_listusers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    caller_id = update.effective_user.id
    if not _is_admin(caller_id):
        await update.message.reply_text("⛔ Нет доступа.")
        return

    total = await count_users()
    if total == 0:
        await update.message.reply_text("📭 Пользователей пока нет.")
        return

    users = await get_users_page(offset=0, limit=PAGE_SIZE)
    text, keyboard = _build_list_page(users, page=0, total=total)
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)


async def handle_list_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    caller_id = query.from_user.id
    if not _is_admin(caller_id):
        await query.edit_message_text("⛔ Нет доступа.")
        return

    page = int(query.data.split(":")[-1])
    total = await count_users()
    users = await get_users_page(offset=page * PAGE_SIZE, limit=PAGE_SIZE)
    text, keyboard = _build_list_page(users, page=page, total=total)
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)


# ── /resettraffic ─────────────────────────────────────────────────────────────

async def cmd_resettraffic(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    caller_id = update.effective_user.id
    if not _is_admin(caller_id):
        await update.message.reply_text("⛔ Нет доступа.")
        return

    args = context.args
    if not args:
        await update.message.reply_text(
            "Использование: `/resettraffic <telegram_id>`", parse_mode="Markdown"
        )
        return

    try:
        target_id = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ telegram_id должен быть числом.")
        return

    db_user = await get_user(target_id)
    if not db_user:
        await update.message.reply_text(
            f"❌ Пользователь `{target_id}` не зарегистрирован.", parse_mode="Markdown"
        )
        return

    client: MarzbanClient = context.bot_data["marzban"]
    try:
        await client.reset_traffic(db_user["marzban_username"])
        await update.message.reply_text(
            f"♻️ Трафик сброшен для `{target_id}` (`{db_user['marzban_username']}`).",
            parse_mode="Markdown",
        )
        logger.info(
            "Admin %d reset traffic for TG %d → Marzban %s",
            caller_id, target_id, db_user["marzban_username"],
        )
    except Exception as exc:
        logger.exception("Error in /resettraffic: %s", exc)
        await update.message.reply_text(f"❌ Ошибка: {exc}")


# ── /broadcast ────────────────────────────────────────────────────────────────

async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    caller_id = update.effective_user.id
    if not _is_admin(caller_id):
        await update.message.reply_text("⛔ Нет доступа.")
        return

    args = context.args
    if not args:
        await update.message.reply_text(
            "Использование: `/broadcast <текст сообщения>`", parse_mode="Markdown"
        )
        return

    message = " ".join(args)
    all_users = await get_all_users()
    if not all_users:
        await update.message.reply_text("📭 Нет пользователей для рассылки.")
        return

    sent, failed = 0, 0
    for user in all_users:
        try:
            await context.bot.send_message(chat_id=user["telegram_id"], text=message)
            sent += 1
        except Exception as exc:
            logger.warning("Broadcast failed for TG %d: %s", user["telegram_id"], exc)
            failed += 1

    await update.message.reply_text(
        f"📣 Рассылка завершена: ✅ отправлено {sent}, ❌ ошибок {failed}."
    )
    logger.info("Admin %d broadcast to %d users (%d failed)", caller_id, sent, failed)


# ── /requests + callback ──────────────────────────────────────────────────────

def _h(text: str) -> str:
    """Escape HTML special chars for safe display."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _requests_text_and_keyboard(
    pending: list[dict],
) -> tuple[str, InlineKeyboardMarkup]:
    if not pending:
        return "📭 Нет ожидающих заявок.", InlineKeyboardMarkup([[
            InlineKeyboardButton("⬅️ Меню", callback_data="admin_menu"),
        ]])

    lines = [f"📋 <b>Заявки на доступ ({len(pending)})</b>\n"]
    buttons: list[list[InlineKeyboardButton]] = []

    for i, req in enumerate(pending, 1):
        uname = f"@{_h(req['tg_username'])}" if req["tg_username"] else "без username"
        name = _h(req["full_name"] or "Без имени")
        date = (req.get("created_at") or "")[:10]
        lines.append(
            f"{i}. <b>{name}</b> ({uname})\n"
            f"   🆔 <code>{req['telegram_id']}</code> · {date}"
        )
        buttons.append([
            InlineKeyboardButton(f"✅ {i}. Принять",   callback_data=f"req_accept:{req['telegram_id']}"),
            InlineKeyboardButton(f"❌ {i}. Отклонить", callback_data=f"req_reject:{req['telegram_id']}"),
        ])

    buttons.append([InlineKeyboardButton("⬅️ Меню", callback_data="admin_menu")])
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


async def cmd_requests(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    caller_id = update.effective_user.id
    if not _is_admin(caller_id):
        await update.message.reply_text("⛔ Нет доступа.")
        return

    pending = await get_pending_requests()
    text, keyboard = _requests_text_and_keyboard(pending)
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)


async def handle_request_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    caller_id = query.from_user.id

    if not _is_admin(caller_id):
        await query.answer("⛔ Нет доступа.", show_alert=True)
        return

    action, raw_id = query.data.split(":")
    target_id = int(raw_id)

    if action == "req_reject":
        await query.answer("Отклонено")
        await update_request_status(target_id, "rejected")
        try:
            await context.bot.send_message(
                chat_id=target_id,
                text="❌ *Ваша заявка отклонена.*\n\nЕсли вы считаете это ошибкой — напишите @Hellylo.",
                parse_mode="Markdown",
            )
        except Exception as exc:
            logger.warning("Failed to notify user %d on reject: %s", target_id, exc)
        logger.info("Admin %d rejected request from TG %d", caller_id, target_id)
        # refresh the list
        pending = await get_pending_requests()
        text, keyboard = _requests_text_and_keyboard(pending)
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)
        return

    # req_accept — auto-create Marzban user and add to DB
    await query.answer("Обрабатываю…")

    req = await get_request(target_id)
    if not req:
        await query.answer("Заявка не найдена", show_alert=True)
        return

    existing = await get_user(target_id)
    if existing:
        await update_request_status(target_id, "accepted")
        pending = await get_pending_requests()
        text, keyboard = _requests_text_and_keyboard(pending)
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)
        return

    raw_username = req.get("tg_username") or f"id{target_id}"
    marzban_username = re.sub(r"[^a-zA-Z0-9_-]", "_", raw_username)[:32]
    if len(marzban_username) < 3:
        marzban_username = f"id{target_id}"

    client: MarzbanClient = context.bot_data["marzban"]
    try:
        try:
            marzban_user = await client.get_user(marzban_username)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                marzban_user = await client.create_user(marzban_username)
            else:
                raise

        link = client.vless_link(marzban_user)
        note = req.get("full_name") or ""
        await add_user(target_id, marzban_username, caller_id, note)
        await update_request_status(target_id, "accepted")

        try:
            await context.bot.send_message(
                chat_id=target_id,
                text=(
                    f"✅ <b>Доступ выдан!</b>\n\n"
                    f"Ваш VLESS конфиг:\n<code>{link}</code>\n\n"
                    "<i>Скопируйте и вставьте в VPN-клиент (v2rayNG, Hiddify, Streisand и др.)</i>"
                ),
                parse_mode="HTML",
            )
        except Exception as exc:
            logger.warning("Failed to send config to user %d: %s", target_id, exc)

        logger.info("Admin %d accepted & added TG %d → Marzban %s", caller_id, target_id, marzban_username)

        pending = await get_pending_requests()
        text, keyboard = _requests_text_and_keyboard(pending)
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)

    except Exception as exc:
        logger.exception("Error accepting request for %d: %s", target_id, exc)
        await query.answer(f"Ошибка: {exc}", show_alert=True)


# ── /mylink ───────────────────────────────────────────────────────────────────

async def _send_mylink(marzban_username: str, client: MarzbanClient, reply_fn) -> None:
    try:
        mu = await client.get_user(marzban_username)
        link = client.vless_link(mu)
        status = mu.get("status", "unknown")
        text = (
            f"🔑 *VLESS конфиг для* `{marzban_username}`\n"
            f"Статус: {_status_emoji(status)} {status}\n\n"
            f"`{link}`"
        )
        await reply_fn(text, parse_mode="Markdown")
    except Exception as exc:
        logger.exception("Error in mylink: %s", exc)
        await reply_fn(f"❌ Ошибка: {exc}")


async def cmd_mylink(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    caller_id = update.effective_user.id
    if not _is_admin(caller_id):
        await update.message.reply_text("⛔ Нет доступа.")
        return

    if context.args:
        marzban_username = context.args[0]
    else:
        db_user = await get_user(caller_id)
        if not db_user:
            await update.message.reply_text(
                "Укажи marzban-юзернейм:\n`/mylink <marzban_username>`",
                parse_mode="Markdown",
            )
            return
        marzban_username = db_user["marzban_username"]

    client: MarzbanClient = context.bot_data["marzban"]
    await _send_mylink(marzban_username, client, update.message.reply_text)


# ── admin menu callbacks ───────────────────────────────────────────────────────

async def handle_admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from handlers.user import admin_menu_keyboard
    query = update.callback_query
    if not _is_admin(query.from_user.id):
        await query.answer("⛔ Нет доступа.", show_alert=True)
        return
    await query.answer()
    await query.edit_message_text(
        "👋 *Панель администратора*\n\nВыберите действие:",
        parse_mode="Markdown",
        reply_markup=admin_menu_keyboard(),
    )


async def handle_admin_requests_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not _is_admin(query.from_user.id):
        await query.answer("⛔ Нет доступа.", show_alert=True)
        return
    await query.answer()
    pending = await get_pending_requests()
    text, keyboard = _requests_text_and_keyboard(pending)
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)


async def handle_admin_listusers_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not _is_admin(query.from_user.id):
        await query.answer("⛔ Нет доступа.", show_alert=True)
        return
    await query.answer()
    page = int(query.data.split(":")[-1])
    total = await count_users()
    if total == 0:
        await query.edit_message_text(
            "📭 Пользователей пока нет.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Меню", callback_data="admin_menu")
            ]]),
        )
        return
    users = await get_users_page(offset=page * PAGE_SIZE, limit=PAGE_SIZE)
    text, keyboard = _build_list_page(users, page=page, total=total)
    existing_rows = [row for row in keyboard.inline_keyboard if row]
    rows = existing_rows + [[InlineKeyboardButton("⬅️ Меню", callback_data="admin_menu")]]
    await query.edit_message_text(
        text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows)
    )


async def handle_admin_mylink_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    caller_id = query.from_user.id
    if not _is_admin(caller_id):
        await query.answer("⛔ Нет доступа.", show_alert=True)
        return
    await query.answer()

    db_user = await get_user(caller_id)
    back_kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Меню", callback_data="admin_menu")]])

    if not db_user:
        await query.edit_message_text(
            "Укажи marzban-юзернейм командой:\n`/mylink <marzban_username>`",
            parse_mode="Markdown",
            reply_markup=back_kb,
        )
        return

    client: MarzbanClient = context.bot_data["marzban"]
    try:
        mu = await client.get_user(db_user["marzban_username"])
        link = client.vless_link(mu)
        status = mu.get("status", "unknown")
        text = (
            f"🔑 *VLESS конфиг для* `{db_user['marzban_username']}`\n"
            f"Статус: {_status_emoji(status)} {status}\n\n"
            f"`{link}`"
        )
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=back_kb)
    except Exception as exc:
        await query.edit_message_text(f"❌ Ошибка: {exc}", reply_markup=back_kb)


async def handle_admin_broadcast_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not _is_admin(query.from_user.id):
        await query.answer("⛔ Нет доступа.", show_alert=True)
        return
    await query.answer()
    await query.edit_message_text(
        "📢 *Рассылка*\n\nОтправьте команду:\n`/broadcast <текст сообщения>`",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("⬅️ Меню", callback_data="admin_menu")
        ]]),
    )
