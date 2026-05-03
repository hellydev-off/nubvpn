import logging
import os
from datetime import datetime, timezone

from telegram import Update
from telegram.ext import ContextTypes

from db import get_user
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

_NO_ACCESS = "❌ You don't have access. Contact the administrator."


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


# ── /start ────────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    db_user = await get_user(user_id)

    if not db_user:
        if user_id in ADMIN_IDS:
            await update.message.reply_text(
                "👋 *Welcome, admin!*\n\n"
                "Available commands:\n"
                "/adduser — Add a user\n"
                "/removeuser — Remove a user\n"
                "/userinfo — Show user details\n"
                "/listusers — List all users\n"
                "/resettraffic — Reset user traffic\n"
                "/broadcast — Send message to all users",
                parse_mode="Markdown",
            )
        else:
            await update.message.reply_text(_NO_ACCESS)
        return

    note = db_user.get("note")
    greeting = f"👋 Hello, *{note}*!" if note else "👋 Hello!"
    await update.message.reply_text(
        f"{greeting}\n\n"
        "Available commands:\n"
        "/sub — Get your VPN subscription link\n"
        "/info — View your account status",
        parse_mode="Markdown",
    )


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
        sub_url = mu.get("subscription_url", "N/A")
        await update.message.reply_text(
            f"Your subscription link:\n`{sub_url}`\n\n"
            "_Copy this link and add it to your VPN client "
            "(Hiddify, Streisand, v2rayNG, etc.)_",
            parse_mode="Markdown",
        )
    except Exception as exc:
        logger.exception("Error in /sub for TG %d: %s", user_id, exc)
        await update.message.reply_text(f"❌ Error retrieving subscription: {exc}")


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
            f"{_status_emoji(status)} *Status:* {status}\n"
            f"📊 *Traffic:* {_fmt_bytes(mu.get('used_traffic'))} / {_fmt_limit(mu.get('data_limit'))}\n"
            f"📅 *Expires:* {_fmt_expire(mu.get('expire'))}"
        )
        await update.message.reply_text(text, parse_mode="Markdown")
    except Exception as exc:
        logger.exception("Error in /info for TG %d: %s", user_id, exc)
        await update.message.reply_text(f"❌ Error retrieving account info: {exc}")
