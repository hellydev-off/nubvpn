import logging
import os

from dotenv import load_dotenv

# load_dotenv MUST run before importing handler modules — they read env at module level
load_dotenv()

from telegram.ext import Application, CallbackQueryHandler, CommandHandler

from db import init_db
from handlers.admin import (
    cmd_adduser,
    cmd_broadcast,
    cmd_devices,
    cmd_listusers,
    cmd_mylink,
    cmd_online,
    cmd_removeuser,
    cmd_requests,
    cmd_resettraffic,
    cmd_stats,
    cmd_userinfo,
    handle_admin_broadcast_callback,
    handle_admin_listusers_callback,
    handle_admin_menu,
    handle_admin_mylink_callback,
    handle_admin_online_callback,
    handle_admin_requests_callback,
    handle_admin_stats_callback,
    handle_devices_callback,
    handle_list_callback,
    handle_remove_callback,
    handle_request_action,
)
from handlers.user import cmd_info, cmd_start, cmd_sub, handle_rules_callback, handle_request_submit
from marzban import MarzbanClient

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


async def post_init(application: Application) -> None:
    await init_db()
    application.bot_data["marzban"] = MarzbanClient(
        base_url=os.environ["MARZBAN_URL"],
        username=os.environ["MARZBAN_USERNAME"],
        password=os.environ["MARZBAN_PASSWORD"],
    )
    logger.info("Database initialised and Marzban client ready")


async def post_shutdown(application: Application) -> None:
    client: MarzbanClient | None = application.bot_data.get("marzban")
    if client:
        await client.close()


def main() -> None:
    token = os.environ.get("TELEGRAM_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_TOKEN is not set")

    app = (
        Application.builder()
        .token(token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # Admin commands
    app.add_handler(CommandHandler("mylink", cmd_mylink))
    app.add_handler(CommandHandler("requests", cmd_requests))
    app.add_handler(CommandHandler("adduser", cmd_adduser))
    app.add_handler(CommandHandler("removeuser", cmd_removeuser))
    app.add_handler(CommandHandler("userinfo", cmd_userinfo))
    app.add_handler(CommandHandler("listusers", cmd_listusers))
    app.add_handler(CommandHandler("resettraffic", cmd_resettraffic))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("online", cmd_online))
    app.add_handler(CommandHandler("devices", cmd_devices))

    # User commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("sub", cmd_sub))
    app.add_handler(CommandHandler("info", cmd_info))

    # Inline keyboard callbacks
    app.add_handler(CallbackQueryHandler(handle_rules_callback,             pattern=r"^rules_accepted$"))
    app.add_handler(CallbackQueryHandler(handle_request_submit,             pattern=r"^send_request$"))
    app.add_handler(CallbackQueryHandler(handle_request_action,             pattern=r"^req_(accept|reject):\d+$"))
    app.add_handler(CallbackQueryHandler(handle_remove_callback,            pattern=r"^(confirm|cancel)_remove:\d+$"))
    app.add_handler(CallbackQueryHandler(handle_list_callback,              pattern=r"^listusers:page:\d+$"))
    app.add_handler(CallbackQueryHandler(handle_admin_menu,                 pattern=r"^admin_menu$"))
    app.add_handler(CallbackQueryHandler(handle_admin_requests_callback,    pattern=r"^admin_requests$"))
    app.add_handler(CallbackQueryHandler(handle_admin_listusers_callback,   pattern=r"^admin_listusers:\d+$"))
    app.add_handler(CallbackQueryHandler(handle_admin_mylink_callback,      pattern=r"^admin_mylink$"))
    app.add_handler(CallbackQueryHandler(handle_admin_broadcast_callback,   pattern=r"^admin_broadcast$"))
    app.add_handler(CallbackQueryHandler(handle_admin_stats_callback,       pattern=r"^admin_stats$"))
    app.add_handler(CallbackQueryHandler(handle_admin_online_callback,      pattern=r"^admin_online$"))
    app.add_handler(CallbackQueryHandler(handle_devices_callback,           pattern=r"^devices:\d+$"))

    logger.info("Bot starting…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
