# telegram-service/app/main.py
import asyncio
import logging
import signal
import sys
from typing import Optional, Tuple

from telethon import TelegramClient

from app.config import (
    API_ID,
    API_HASH,
    PROXY_HOST,
    PROXY_PORT,
    fetch_bot_credentials,
)
from app.handlers.start import register_start_handler
from app.handlers.balance import register_balance_handlers
from app.handlers.configs import register_configs_handlers
from app.handlers.support import register_support_handlers
from app.handlers.analytics import register_analytics_handlers
from app.events.consumer import start_rabbitmq_consumer
from vpn_shared.logger import setup_logging

logger = setup_logging("telegram-service")


def get_telethon_proxy_config() -> Optional[Tuple[str, str, int]]:
    """
    Configures an outbound proxy for Telethon MTProto connection if PROXY_HOST
    and PROXY_PORT are specified in environment variables.
    """
    if PROXY_HOST and PROXY_PORT:
        try:
            port = int(PROXY_PORT)
            logger.info("Configuring Telethon outbound proxy via %s:%d (HTTP)", PROXY_HOST, port)
            return ("http", PROXY_HOST, port)
        except ValueError:
            logger.warning("Invalid PROXY_PORT specified ('%s'). Connecting directly.", PROXY_PORT)
    return None


async def main():
    # 1. Verify required Telegram application credentials
    if not API_ID or not API_HASH:
        logger.critical(
            "TELEGRAM_API_ID or TELEGRAM_API_HASH is missing from environment. "
            "Please configure them in your .env file or docker-compose."
        )
        sys.exit(1)

    # 2. Fetch dynamic bot token set by Admin or fall back to .env
    bot_token, bot_username = await fetch_bot_credentials()
    if not bot_token:
        logger.critical(
            "No Bot Token found in Notification Service settings or .env (TELEGRAM_BOT_TOKEN). "
            "Please provide a valid bot token to start."
        )
        sys.exit(1)

    logger.info("Initializing Telethon client for bot @%s...", bot_username)

    proxy_config = get_telethon_proxy_config()

    # 3. Initialize Telethon client in Bot mode
    bot = TelegramClient(
        session="payg_bot_session",
        api_id=API_ID,
        api_hash=API_HASH,
        proxy=proxy_config,
    )

    try:
        await bot.start(bot_token=bot_token)
        me = await bot.get_me()
        logger.info("Telethon Bot successfully authenticated as @%s (ID: %s)", me.username, me.id)
    except Exception as e:
        logger.critical("Failed to authenticate Telethon bot with provided token: %s", e)
        sys.exit(1)

    # 4. Register all command, callback, and multi-step wizard handlers
    await register_start_handler(bot)
    await register_balance_handlers(bot)
    await register_configs_handlers(bot)
    await register_support_handlers(bot)
    await register_analytics_handlers(bot)

    logger.info("Registered all bot commands, FSM wizards, and interactive keyboards.")

    # 5. Set up graceful shutdown handlers
    stop_event = asyncio.Event()

    def handle_signal(sig):
        logger.info("Received exit signal (%s). Initiating graceful shutdown...", sig)
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda s=sig: handle_signal(s))
        except NotImplementedError:
            # Signal handlers are not fully supported on some non-POSIX environments (e.g. Windows)
            pass

    # 6. Run Telethon loop and RabbitMQ push consumer concurrently
    telethon_task = asyncio.create_task(bot.run_until_disconnected(), name="telethon_worker")
    rabbitmq_task = asyncio.create_task(start_rabbitmq_consumer(bot), name="rabbitmq_consumer")

    async def wait_for_stop():
        await stop_event.wait()
        telethon_task.cancel()
        rabbitmq_task.cancel()

    stop_task = asyncio.create_task(wait_for_stop(), name="stop_listener")

    try:
        await asyncio.gather(telethon_task, rabbitmq_task, stop_task)
    except asyncio.CancelledError:
        logger.info("Core async tasks cancelled for shutdown.")
    except Exception as e:
        logger.error("Unexpected error in main bot loop: %s", e)
    finally:
        logger.info("Disconnecting Telethon client session...")
        try:
            await bot.disconnect()
        except Exception as e:
            logger.warning("Error during bot disconnection: %s", e)
        logger.info("Telegram Bot service successfully stopped.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Process terminated.")