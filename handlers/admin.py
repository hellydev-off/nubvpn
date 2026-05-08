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
XRAY_LOG = "/var/lib/marzban/access.log"
_LOG_RE = re.compile(
    r"^(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}) from ([\d.:a-fA-F]+):\d+ \S+ \S+ \[[^\]]+\] email: \d+\.(\S+)$"
)

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
    return "Без лимита" if not b else _fmt_bytes(b)


def _fmt_expire(ts: int | None) -> str:
    if ts is None:
        return "Бессрочно"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def _status_emoji(status: str) -> str:
    return _STATUS_EMOJI.get(status, "❓")


def _time_ago(ts: str | int | None) -> str:
    if ts is None:
        return "никогда"
    try:
        if isinstance(ts, (int, float)):
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        else:
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
    except Exception:
        return str(ts)
    diff = int((datetime.now(timezone.utc) - dt).total_seconds())
    if diff < 60:
        return "только что"
    if diff < 3600:
        return f"{diff // 60} мин. назад"
    if diff < 86400:
        return f"{diff // 3600} ч. назад"
    if diff < 86400 * 30:
        return f"{diff // 86400} дн. назад"
    return dt.strftime("%Y-%m-%d")


def _fmt_client(agent: str | None) -> str:
    if not agent:
        return "неизвестно"
    # shorten long UA strings
    for app in ("v2rayNG", "Hiddify", "Streisand", "Nekoray", "Clash", "sing-box", "Shadowrocket"):
        if app.lower() in agent.lower():
            return app
    return agent[:30]


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
        status = mu.get("status", "unknown")
        text = (
            f"<b>ℹ️ Информация о пользователе</b>\n\n"
            f"Telegram ID: <code>{target_id}</code>\n"
            f"Marzban: <code>{_h(mu.get('username', ''))}</code>\n"
            f"Заметка: {_h(db_user.get('note') or '—')}\n\n"
            f"{_status_emoji(status)} <b>Статус:</b> {status}\n"
            f"📊 <b>Трафик:</b> {_fmt_bytes(mu.get('used_traffic'))} / {_fmt_limit(mu.get('data_limit'))}\n"
            f"📅 <b>Истекает:</b> {_fmt_expire(mu.get('expire'))}\n"
            f"🕐 <b>Онлайн:</b> {_time_ago(mu.get('online_at'))}\n"
            f"📱 <b>Клиент:</b> {_h(_fmt_client(mu.get('sub_last_user_agent')))}\n"
            f"🔄 <b>Подписка обновлена:</b> {_time_ago(mu.get('sub_updated_at'))}\n\n"
            f"Добавил: <code>{db_user.get('added_by')}</code>\n"
            f"Дата добавления: {(db_user.get('added_at') or '')[:10]}"
        )
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("📱 Устройства / IP", callback_data=f"devices:{target_id}")
        ]])
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)
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

    sent, failed_list = 0, []
    for user in all_users:
        try:
            await context.bot.send_message(chat_id=user["telegram_id"], text=message)
            sent += 1
        except Exception as exc:
            reason = str(exc)
            logger.warning("Broadcast failed for TG %d: %s", user["telegram_id"], exc)
            failed_list.append((user["telegram_id"], user.get("marzban_username", "?"), reason))

    lines = [f"📣 <b>Рассылка завершена</b>: ✅ {sent} / {len(all_users)}"]
    if failed_list:
        lines.append(f"\n❌ <b>Не доставлено ({len(failed_list)}):</b>")
        for tid, uname, reason in failed_list:
            if "not found" in reason.lower() or "bot was blocked" in reason.lower():
                hint = "не начал чат с ботом или заблокировал"
            else:
                hint = reason
            lines.append(f"  • <code>{tid}</code> ({uname}) — {hint}")
        lines.append(
            "\n<i>💡 Пользователь получит сообщение только если сам писал боту хотя бы раз.</i>"
        )

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")
    logger.info("Admin %d broadcast to %d users (%d failed)", caller_id, sent, len(failed_list))


# ── /stats ────────────────────────────────────────────────────────────────────

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    caller_id = update.effective_user.id
    if not _is_admin(caller_id):
        await update.message.reply_text("⛔ Нет доступа.")
        return

    db_users = await get_all_users()
    if not db_users:
        await update.message.reply_text("📭 Пользователей нет.")
        return

    client: MarzbanClient = context.bot_data["marzban"]
    rows: list[dict] = []
    for u in db_users:
        try:
            mu = await client.get_user(u["marzban_username"])
            rows.append({
                "telegram_id": u["telegram_id"],
                "username": mu.get("username", u["marzban_username"]),
                "note": u.get("note") or "",
                "status": mu.get("status", "unknown"),
                "used": mu.get("used_traffic") or 0,
                "limit": mu.get("data_limit") or 0,
                "online_at": mu.get("online_at"),
                "client": _fmt_client(mu.get("sub_last_user_agent")),
            })
        except Exception:
            rows.append({
                "telegram_id": u["telegram_id"],
                "username": u["marzban_username"],
                "note": u.get("note") or "",
                "status": "unknown",
                "used": 0, "limit": 0,
                "online_at": None, "client": "?",
            })

    rows.sort(key=lambda r: r["used"], reverse=True)
    total_used = sum(r["used"] for r in rows)

    lines = [f"<b>📊 Статистика трафика ({len(rows)} польз.)</b>\n"]
    for i, r in enumerate(rows, 1):
        bar = "█" * min(10, int(r["used"] / max(total_used, 1) * 10)) if total_used else ""
        note = f" · {_h(r['note'])}" if r["note"] else ""
        lines.append(
            f"{i}. {_status_emoji(r['status'])} <code>{_h(r['username'])}</code>{note}\n"
            f"   {_fmt_bytes(r['used'])} / {_fmt_limit(r['limit'])}  {bar}\n"
            f"   🕐 {_time_ago(r['online_at'])}  📱 {_h(r['client'])}"
        )

    lines.append(f"\n<b>Итого:</b> {_fmt_bytes(total_used)}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# ── /online ────────────────────────────────────────────────────────────────────

async def cmd_online(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    caller_id = update.effective_user.id
    if not _is_admin(caller_id):
        await update.message.reply_text("⛔ Нет доступа.")
        return

    db_users = await get_all_users()
    if not db_users:
        await update.message.reply_text("📭 Пользователей нет.")
        return

    client: MarzbanClient = context.bot_data["marzban"]
    now = datetime.now(timezone.utc)
    online_rows: list[tuple] = []

    for u in db_users:
        try:
            mu = await client.get_user(u["marzban_username"])
            ts = mu.get("online_at")
            if ts is None:
                continue
            try:
                dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
            except Exception:
                continue
            diff = (now - dt).total_seconds()
            online_rows.append((diff, mu.get("username", u["marzban_username"]),
                                 mu.get("status", "unknown"),
                                 _fmt_client(mu.get("sub_last_user_agent")),
                                 _fmt_bytes(mu.get("used_traffic") or 0)))
        except Exception:
            continue

    online_rows.sort(key=lambda r: r[0])

    if not online_rows:
        await update.message.reply_text("😴 Никто не был онлайн (данных нет).")
        return

    h24 = [r for r in online_rows if r[0] <= 86400]
    older = [r for r in online_rows if r[0] > 86400]

    lines = [f"<b>🟢 Активность пользователей</b>\n"]
    if h24:
        lines.append("<b>За последние 24ч:</b>")
        for diff, uname, status, client_app, traffic in h24:
            lines.append(
                f"  {_status_emoji(status)} <code>{_h(uname)}</code> — "
                f"{_time_ago_s(diff)}  📱 {_h(client_app)}  📊 {traffic}"
            )
    if older:
        lines.append("\n<b>Ранее:</b>")
        for diff, uname, status, client_app, traffic in older:
            lines.append(
                f"  {_status_emoji(status)} <code>{_h(uname)}</code> — "
                f"{_time_ago_s(diff)}"
            )

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


def _time_ago_s(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return "только что"
    if s < 3600:
        return f"{s // 60} мин. назад"
    if s < 86400:
        return f"{s // 3600} ч. назад"
    return f"{s // 86400} дн. назад"


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


# ── /devices ─────────────────────────────────────────────────────────────────

def _parse_access_log(marzban_username: str, max_lines: int = 300_000) -> dict[str, dict]:
    """Return {ip: {count, first, last}} from Xray access log for one user."""
    result: dict[str, dict] = {}
    try:
        with open(XRAY_LOG, "r", errors="ignore") as f:
            lines = f.readlines()[-max_lines:]
    except FileNotFoundError:
        return {}
    uname_lower = marzban_username.lower()
    for line in lines:
        m = _LOG_RE.match(line.strip())
        if not m:
            continue
        ts_str, ip, email = m.group(1), m.group(2), m.group(3)
        if email.lower() != uname_lower:
            continue
        try:
            dt = datetime.strptime(ts_str, "%Y/%m/%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if ip not in result:
            result[ip] = {"count": 0, "first": dt, "last": dt}
        result[ip]["count"] += 1
        if dt < result[ip]["first"]:
            result[ip]["first"] = dt
        if dt > result[ip]["last"]:
            result[ip]["last"] = dt
    return result


async def cmd_devices(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    caller_id = update.effective_user.id
    if not _is_admin(caller_id):
        await update.message.reply_text("⛔ Нет доступа.")
        return

    args = context.args
    if not args:
        await update.message.reply_text(
            "Использование: <code>/devices &lt;telegram_id&gt;</code>", parse_mode="HTML"
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
            f"❌ Пользователь <code>{target_id}</code> не найден.", parse_mode="HTML"
        )
        return

    await update.message.reply_text("⏳ Анализирую лог…")
    await _send_devices(db_user["marzban_username"], update.message.reply_text)


async def handle_devices_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not _is_admin(query.from_user.id):
        await query.answer("⛔ Нет доступа.", show_alert=True)
        return
    await query.answer("Анализирую лог…")

    target_id = int(query.data.split(":")[1])
    db_user = await get_user(target_id)
    if not db_user:
        await query.answer("Пользователь не найден.", show_alert=True)
        return

    await _send_devices(db_user["marzban_username"], query.message.reply_text)


async def _send_devices(marzban_username: str, reply_fn) -> None:
    ips = _parse_access_log(marzban_username)

    if not ips:
        await reply_fn(
            f"📭 Нет данных о подключениях для <code>{_h(marzban_username)}</code>.\n"
            "<i>Лог пишется с момента последнего запуска Xray.</i>",
            parse_mode="HTML",
        )
        return

    sorted_ips = sorted(ips.items(), key=lambda x: x[1]["last"], reverse=True)
    total_conn = sum(d["count"] for d in ips.values())

    lines = [
        f"📱 <b>Подключения для</b> <code>{_h(marzban_username)}</code>\n"
        f"Уникальных IP: <b>{len(sorted_ips)}</b>  ·  Всего соединений: <b>{total_conn}</b>"
    ]

    if len(sorted_ips) > 1:
        lines.append(f"\n⚠️ <b>{len(sorted_ips)} разных IP — возможна раздача ссылки!</b>")

    for ip, data in sorted_ips:
        lines.append(
            f"\n🔹 <code>{ip}</code>\n"
            f"   Соединений: {data['count']}\n"
            f"   Первый раз: {data['first'].strftime('%Y-%m-%d %H:%M UTC')}\n"
            f"   Последний раз: {data['last'].strftime('%Y-%m-%d %H:%M UTC')}"
        )

    lines.append("\n<i>Данные из лога Xray с момента последнего перезапуска.</i>")
    await reply_fn("\n".join(lines), parse_mode="HTML")


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


async def handle_admin_stats_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not _is_admin(query.from_user.id):
        await query.answer("⛔ Нет доступа.", show_alert=True)
        return
    await query.answer("Загружаю статистику…")
    await query.edit_message_text("⏳ Загружаю данные…")

    db_users = await get_all_users()
    client: MarzbanClient = context.bot_data["marzban"]
    rows: list[dict] = []
    for u in db_users:
        try:
            mu = await client.get_user(u["marzban_username"])
            rows.append({
                "username": mu.get("username", u["marzban_username"]),
                "note": u.get("note") or "",
                "status": mu.get("status", "unknown"),
                "used": mu.get("used_traffic") or 0,
                "limit": mu.get("data_limit") or 0,
                "online_at": mu.get("online_at"),
                "client": _fmt_client(mu.get("sub_last_user_agent")),
            })
        except Exception:
            rows.append({"username": u["marzban_username"], "note": u.get("note") or "",
                         "status": "unknown", "used": 0, "limit": 0,
                         "online_at": None, "client": "?"})

    rows.sort(key=lambda r: r["used"], reverse=True)
    total_used = sum(r["used"] for r in rows)

    lines = [f"<b>📊 Статистика трафика ({len(rows)} польз.)</b>\n"]
    for i, r in enumerate(rows, 1):
        bar = "█" * min(10, int(r["used"] / max(total_used, 1) * 10)) if total_used else ""
        note = f" · {_h(r['note'])}" if r["note"] else ""
        lines.append(
            f"{i}. {_status_emoji(r['status'])} <code>{_h(r['username'])}</code>{note}\n"
            f"   {_fmt_bytes(r['used'])} / {_fmt_limit(r['limit'])}  {bar}\n"
            f"   🕐 {_time_ago(r['online_at'])}  📱 {_h(r['client'])}"
        )
    lines.append(f"\n<b>Итого:</b> {_fmt_bytes(total_used)}")

    await query.edit_message_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Меню", callback_data="admin_menu")]]),
    )


async def handle_admin_online_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not _is_admin(query.from_user.id):
        await query.answer("⛔ Нет доступа.", show_alert=True)
        return
    await query.answer()
    await query.edit_message_text("⏳ Загружаю данные…")

    db_users = await get_all_users()
    client: MarzbanClient = context.bot_data["marzban"]
    now = datetime.now(timezone.utc)
    online_rows: list[tuple] = []

    for u in db_users:
        try:
            mu = await client.get_user(u["marzban_username"])
            ts = mu.get("online_at")
            if ts is None:
                continue
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            diff = (now - dt).total_seconds()
            online_rows.append((diff, mu.get("username", u["marzban_username"]),
                                 mu.get("status", "unknown"),
                                 _fmt_client(mu.get("sub_last_user_agent")),
                                 _fmt_bytes(mu.get("used_traffic") or 0)))
        except Exception:
            continue

    back_kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Меню", callback_data="admin_menu")]])

    if not online_rows:
        await query.edit_message_text("😴 Данных об активности нет.", reply_markup=back_kb)
        return

    online_rows.sort(key=lambda r: r[0])
    h24 = [r for r in online_rows if r[0] <= 86400]
    older = [r for r in online_rows if r[0] > 86400]

    lines = ["<b>🟢 Активность пользователей</b>\n"]
    if h24:
        lines.append("<b>За последние 24ч:</b>")
        for diff, uname, status, client_app, traffic in h24:
            lines.append(
                f"  {_status_emoji(status)} <code>{_h(uname)}</code> — "
                f"{_time_ago_s(diff)}  📱 {_h(client_app)}  📊 {traffic}"
            )
    if older:
        lines.append("\n<b>Ранее:</b>")
        for diff, uname, status, client_app, traffic in older:
            lines.append(f"  {_status_emoji(status)} <code>{_h(uname)}</code> — {_time_ago_s(diff)}")

    await query.edit_message_text("\n".join(lines), parse_mode="HTML", reply_markup=back_kb)
