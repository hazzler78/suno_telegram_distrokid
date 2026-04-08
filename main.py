from __future__ import annotations

import logging

from config.settings import settings


def configure_logging() -> None:
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )


def main() -> None:
    from telegram_bot.bot import run_bot

    configure_logging()
    logging.getLogger(__name__).info("Starting Telegram bot...")
    run_bot()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
