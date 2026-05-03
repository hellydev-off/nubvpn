import logging
import math
import os
from datetime import datetime, timezone

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from db import add_user, count_users, get_all_users, get_user, get_users_page, remove_user
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
        await update.message.reply_text("⛔ No access.")
        return

    args = context.args
    if not args or len(args) < 2:
        await update.message.reply_text(
            "Usage: `/adduser <telegram\\_id> <marzban\\_username> [note]`",
            parse_mode="Markdown",
        )
        return

    try:
        target_id = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ telegram\\_id must be a number.", parse_mode="Markdown")
        return

    marzban_username = args[1]
    note = " ".join(args[2:]) if len(args) > 2 else ""

    existing = await get_user(target_id)
    if existing:
        await update.message.reply_text(
            f"⚠️ User `{target_id}` is already registered as `{existing['marzban_username']}`.",
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

        sub_url = marzban_user.get("subscription_url", "N/A")
        await add_user(target_id, marzban_username, caller_id, note)

        text = (
            f"✅ *User added successfully\\!*\n"
            f"Telegram ID: `{target_id}`\n"
            f"Marzban username: `{marzban_username}`\n"
            f"Note: {note or '—'}\n\n"
            f"Subscription URL:\n`{sub_url}`"
        )
        await update.message.reply_text(text, parse_mode="MarkdownV2")
        logger.info("Admin %d added TG %d → Marzban %s", caller_id, target_id, marzban_username)
    except Exception as exc:
        logger.exception("Error in /adduser: %s", exc)
        await update.message.reply_text(f"❌ Error: {exc}")


# ── /removeuser ───────────────────────────────────────────────────────────────

async def cmd_removeuser(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    caller_id = update.effective_user.id
    if not _is_admin(caller_id):
        await update.message.reply_text("⛔ No access.")
        return

    args = context.args
    if not args:
        await update.message.reply_text(
            "Usage: `/removeuser <telegram_id>`", parse_mode="Markdown"
        )
        return

    try:
        target_id = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ telegram_id must be a number.")
        return

    db_user = await get_user(target_id)
    if not db_user:
        await update.message.reply_text(
            f"❌ User `{target_id}` is not registered.", parse_mode="Markdown"
        )
        return

    marzban_username = db_user["marzban_username"]
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "✅ Confirm delete", callback_data=f"confirm_remove:{target_id}"
            ),
            InlineKeyboardButton(
                "❌ Cancel", callback_data=f"cancel_remove:{target_id}"
            ),
        ]
    ])
    await update.message.reply_text(
        f"⚠️ Delete user `{target_id}` (`{marzban_username}`) from Marzban and the database?",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )


async def handle_remove_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    caller_id = query.from_user.id
    if not _is_admin(caller_id):
        await query.edit_message_text("⛔ No access.")
        return

    action, raw_id = query.data.split(":")
    target_id = int(raw_id)

    if action == "cancel_remove":
        await query.edit_message_text("❌ Deletion cancelled.")
        return

    db_user = await get_user(target_id)
    if not db_user:
        await query.edit_message_text("⚠️ User was already removed.")
        return

    marzban_username = db_user["marzban_username"]
    client: MarzbanClient = context.bot_data["marzban"]
    try:
        await client.delete_user(marzban_username)
        await remove_user(target_id)
        await query.edit_message_text(
            f"🗑 User `{target_id}` (`{marzban_username}`) deleted from Marzban and database.",
            parse_mode="Markdown",
        )
        logger.info(
            "Admin %d deleted TG %d → Marzban %s", caller_id, target_id, marzban_username
        )
    except Exception as exc:
        logger.exception("Error deleting user %d: %s", target_id, exc)
        await query.edit_message_text(f"❌ Error: {exc}")


# ── /userinfo ─────────────────────────────────────────────────────────────────

async def cmd_userinfo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    caller_id = update.effective_user.id
    if not _is_admin(caller_id):
        await update.message.reply_text("⛔ No access.")
        return

    args = context.args
    if not args:
        await update.message.reply_text(
            "Usage: `/userinfo <telegram_id>`", parse_mode="Markdown"
        )
        return

    try:
        target_id = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ telegram_id must be a number.")
        return

    db_user = await get_user(target_id)
    if not db_user:
        await update.message.reply_text(
            f"❌ User `{target_id}` is not registered.", parse_mode="Markdown"
        )
        return

    client: MarzbanClient = context.bot_data["marzban"]
    try:
        mu = await client.get_user(db_user["marzban_username"])
        status = mu.get("status", "unknown")
        text = (
            f"*User info*\n"
            f"Telegram ID: `{target_id}`\n"
            f"Marzban: `{mu.get('username')}`\n"
            f"Note: {db_user.get('note') or '—'}\n"
            f"Status: {_status_emoji(status)} {status}\n"
            f"Traffic: {_fmt_bytes(mu.get('used_traffic'))} / {_fmt_limit(mu.get('data_limit'))}\n"
            f"Expires: {_fmt_expire(mu.get('expire'))}\n"
            f"Added by: `{db_user.get('added_by')}`\n"
            f"Added at: {(db_user.get('added_at') or '')[:10]}"
        )
        await update.message.reply_text(text, parse_mode="Markdown")
    except Exception as exc:
        logger.exception("Error in /userinfo: %s", exc)
        await update.message.reply_text(f"❌ Error: {exc}")


# ── /listusers ────────────────────────────────────────────────────────────────

def _build_list_page(
    users: list[dict], page: int, total: int
) -> tuple[str, InlineKeyboardMarkup]:
    total_pages = max(1, math.ceil(total / PAGE_SIZE))
    start = page * PAGE_SIZE + 1
    end = min(start + PAGE_SIZE - 1, total)

    lines = [f"*👥 Users ({start}–{end} of {total})*\n"]
    for u in users:
        note = u.get("note") or "—"
        lines.append(f"`{u['telegram_id']}` | `{u['marzban_username']}` | {note}")
    lines.append(f"\nPage {page + 1}/{total_pages}")

    buttons: list[InlineKeyboardButton] = []
    if page > 0:
        buttons.append(
            InlineKeyboardButton("◀ Prev", callback_data=f"listusers:page:{page - 1}")
        )
    if page + 1 < total_pages:
        buttons.append(
            InlineKeyboardButton("Next ▶", callback_data=f"listusers:page:{page + 1}")
        )

    keyboard = InlineKeyboardMarkup([buttons]) if buttons else InlineKeyboardMarkup([[]])
    return "\n".join(lines), keyboard


async def cmd_listusers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    caller_id = update.effective_user.id
    if not _is_admin(caller_id):
        await update.message.reply_text("⛔ No access.")
        return

    total = await count_users()
    if total == 0:
        await update.message.reply_text("📭 No users registered yet.")
        return

    users = await get_users_page(offset=0, limit=PAGE_SIZE)
    text, keyboard = _build_list_page(users, page=0, total=total)
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)


async def handle_list_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    caller_id = query.from_user.id
    if not _is_admin(caller_id):
        await query.edit_message_text("⛔ No access.")
        return

    page = int(query.data.split(":")[-1])
    total = await count_users()
    users = await get_users_page(offset=page * PAGE_SIZE, limit=PAGE_SIZE)
    text, keyboard = _build_list_page(users, page=page, total=total)
    await query.edit_message_text(text, parse_mode="Markdown", reply_markup=keyboard)


# ── /resettraffic ─────────────────────────────────────────────────────────────

async def cmd_resettraffic(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    caller_id = update.effective_user.id
    if not _is_admin(caller_id):
        await update.message.reply_text("⛔ No access.")
        return

    args = context.args
    if not args:
        await update.message.reply_text(
            "Usage: `/resettraffic <telegram_id>`", parse_mode="Markdown"
        )
        return

    try:
        target_id = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ telegram_id must be a number.")
        return

    db_user = await get_user(target_id)
    if not db_user:
        await update.message.reply_text(
            f"❌ User `{target_id}` is not registered.", parse_mode="Markdown"
        )
        return

    client: MarzbanClient = context.bot_data["marzban"]
    try:
        await client.reset_traffic(db_user["marzban_username"])
        await update.message.reply_text(
            f"♻️ Traffic reset for `{target_id}` (`{db_user['marzban_username']}`).",
            parse_mode="Markdown",
        )
        logger.info(
            "Admin %d reset traffic for TG %d → Marzban %s",
            caller_id, target_id, db_user["marzban_username"],
        )
    except Exception as exc:
        logger.exception("Error in /resettraffic: %s", exc)
        await update.message.reply_text(f"❌ Error: {exc}")


# ── /broadcast ────────────────────────────────────────────────────────────────

async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    caller_id = update.effective_user.id
    if not _is_admin(caller_id):
        await update.message.reply_text("⛔ No access.")
        return

    args = context.args
    if not args:
        await update.message.reply_text(
            "Usage: `/broadcast <message text>`", parse_mode="Markdown"
        )
        return

    message = " ".join(args)
    all_users = await get_all_users()
    if not all_users:
        await update.message.reply_text("📭 No users to broadcast to.")
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
        f"📣 Broadcast complete: ✅ {sent} sent, ❌ {failed} failed."
    )
    logger.info("Admin %d broadcast to %d users (%d failed)", caller_id, sent, failed)
