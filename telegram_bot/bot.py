from __future__ import annotations

import logging
import re
from typing import Optional

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

from config.settings import settings
from utils import get_logger
from suno.downloader import download_from_message
from suno.metadata import parse_metadata_with_ai_fallback
from cover_gen.generator import generate_cover
from distrokid.uploader import upload_release


logger = get_logger(__name__)


SUNO_URL_RE = re.compile(r"https?://(?:www\.)?suno\.ai(?:/song|/track|/listen)?/[\w-]+", re.IGNORECASE)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
	await update.message.reply_text("Send me a Suno link or audio file to process.")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
	message = update.message
	assert message is not None

	await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.TYPING)

	try:
		download_path = await download_from_message(message, context)
		meta = await parse_metadata_with_ai_fallback(download_path)
		cover_path = await generate_cover(meta)
		submitted = await upload_release(audio_path=download_path, cover_path=cover_path, metadata=meta)
		if submitted:
			await message.reply_text("DistroKid submission appears successful (pending processing).")
		else:
			await message.reply_text("DistroKid submission may have failed. Check work/distrokid_submit_failed.png and adjust credentials/selectors.")
	except Exception as exc:
		logger.exception("Processing failed: %s", exc)
		await message.reply_text(f"Failed: {exc}")


def run_bot() -> None:
	application = Application.builder().token(settings.telegram_bot_token).concurrent_updates(True).build()

	application.add_handler(CommandHandler("start", start))
	audio_or_audio_doc = filters.AUDIO | (filters.Document.MimeType("audio/")) | filters.TEXT
	application.add_handler(MessageHandler(audio_or_audio_doc, handle_message))

	logger.info("Bot starting polling")
	application.run_polling(allowed_updates=Update.ALL_TYPES)
