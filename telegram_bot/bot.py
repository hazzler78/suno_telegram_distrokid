from __future__ import annotations

import asyncio

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

from config.settings import settings
from utils import get_logger
from utils.tracker import tracker
from suno.downloader import download_from_message
from suno.metadata import parse_metadata_with_ai_fallback
from cover_gen.generator import generate_cover
from distrokid.packager import package_release
from distrokid.uploader import upload_release


logger = get_logger(__name__)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.message is not None
    await update.message.reply_text(
        "Send a Suno link or audio file.\n"
        "Commands:\n"
        "/history - last tracks\n"
        "/status <id> - details for one track\n"
        "/notes <id> <text> - append notes"
    )


async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.message is not None
    rows = await asyncio.to_thread(tracker.get_song_history, 15)
    if not rows:
        await update.message.reply_text("No tracked songs yet.")
        return
    lines = ["Recent songs:"]
    for row in rows:
        song_label = row.get("song_id") or row["id"]
        title = row.get("title") or "Untitled"
        artist = row.get("artist") or "Unknown Artist"
        lines.append(f"- `{song_label}` | {row['status']} | {title} - {artist}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def last_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.message is not None
    rows = await asyncio.to_thread(tracker.get_song_history, 1)
    if not rows:
        await update.message.reply_text("No tracked songs yet.")
        return
    row = rows[0]
    text = (
        f"Latest run: `{row['id']}`\n"
        f"Song UUID: `{row.get('song_id') or 'N/A'}`\n"
        f"Status: `{row['status']}`\n"
        f"Title: {row.get('title') or 'N/A'}\n"
        f"Artist: {row.get('artist') or 'N/A'}\n"
        f"Audio: {row.get('audio_path') or 'N/A'}\n"
        f"Cover: {row.get('cover_path') or 'N/A'}\n"
        f"Zip: {row.get('zip_path') or 'N/A'}"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.message is not None
    if not context.args:
        await update.message.reply_text("Usage: /status <song_id_or_partial_id>")
        return
    key = context.args[0]
    row = await asyncio.to_thread(tracker.get_song_status, key)
    if not row:
        await update.message.reply_text(f"No song found for `{key}`.", parse_mode="Markdown")
        return
    text = (
        f"ID: `{row['id']}`\n"
        f"Song UUID: `{row.get('song_id') or 'N/A'}`\n"
        f"Status: `{row['status']}`\n"
        f"Title: {row.get('title') or 'N/A'}\n"
        f"Artist: {row.get('artist') or 'N/A'}\n"
        f"Source: {row.get('source_url') or 'N/A'}\n"
        f"Audio: {row.get('audio_path') or 'N/A'}\n"
        f"Cover: {row.get('cover_path') or 'N/A'}\n"
        f"Zip: {row.get('zip_path') or 'N/A'}\n"
        f"Notes: {row.get('notes') or 'None'}"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def notes_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.message is not None
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /notes <song_id_or_partial_id> <note text>")
        return
    key = context.args[0]
    note_text = " ".join(context.args[1:]).strip()
    ok = await asyncio.to_thread(tracker.add_notes, key, note_text)
    if not ok:
        await update.message.reply_text(f"Could not find song `{key}`.", parse_mode="Markdown")
        return
    await update.message.reply_text("Note added.")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    assert message is not None

    track_key: str | None = None
    await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.TYPING)
    try:
        await message.reply_text("Step 1/5: Downloading audio...")
        download_result = await download_from_message(message, context)
        source_meta = {
            **download_result.source_metadata,
            "source_url": download_result.source_url,
            "song_id": download_result.song_id,
        }
        track_key = await asyncio.to_thread(
            tracker.log_song_start,
            song_id=download_result.song_id,
            source_url=download_result.source_url,
            title=download_result.source_metadata.get("title"),
            artist=download_result.source_metadata.get("artist"),
        )
        await asyncio.to_thread(
            tracker.update_song_status,
            track_key,
            "downloaded",
            audio_path=str(download_result.audio_path),
        )
        try:
            size_mb = download_result.audio_path.stat().st_size / (1024 * 1024)
            await message.reply_text(
                f"✅ Step 1/5: Downloaded `{download_result.audio_path.name}` ({size_mb:.2f} MB) (ID: `{track_key}`)",
                parse_mode="Markdown",
            )
        except Exception:
            await message.reply_text(f"✅ Step 1/5: Downloaded (ID: `{track_key}`)", parse_mode="Markdown")

        await message.reply_text("Step 2/5: Extracting metadata...")
        meta = await parse_metadata_with_ai_fallback(download_result.audio_path, source_metadata=source_meta)
        if settings.default_artist_name:
            meta.artist = settings.default_artist_name
        await asyncio.to_thread(
            tracker.update_song_status,
            track_key,
            "metadata_extracted",
            title=meta.title,
            artist=meta.artist,
            song_id=meta.song_id,
        )
        await message.reply_text(f"✅ Step 2/5: Metadata extracted (ID: `{track_key}`)", parse_mode="Markdown")

        await message.reply_text("Step 3/5: Generating 3000x3000 cover...")
        cover_path = await generate_cover(meta)
        await asyncio.to_thread(
            tracker.update_song_status,
            track_key,
            "cover_generated",
            cover_path=str(cover_path),
        )
        await message.reply_text(f"✅ Step 3/5: Cover ready (ID: `{track_key}`)", parse_mode="Markdown")

        await message.reply_text("Step 4/5: Packaging DistroKid-ready folder + zip...")
        package = await package_release(download_result.audio_path, cover_path, meta)
        await asyncio.to_thread(
            tracker.update_song_status,
            track_key,
            "packaged",
            zip_path=str(package.zip_path),
        )
        await message.reply_text(
            "✅ Step 4/5: Package ready\n"
            f"Folder: `{package.output_dir}`\n"
            f"Zip: `{package.zip_path}`\n"
            "Drag this into DistroKid.",
            parse_mode="Markdown",
        )

        await message.reply_text("Step 5/5: Attempting optional DistroKid auto-upload...")
        submitted = await upload_release(
            audio_path=download_result.audio_path,
            cover_path=cover_path,
            metadata=meta,
        )
        await asyncio.to_thread(tracker.update_song_status, track_key, "upload_attempted")
        if submitted:
            await message.reply_text("✅ Step 5/5: Auto-upload appears successful.")
        else:
            await message.reply_text(
                "Auto-upload not confirmed. Manual package is ready and tracked via /status."
            )
    except Exception as exc:
        logger.exception("Processing failed: %s", exc)
        if track_key:
            await asyncio.to_thread(tracker.update_song_status, track_key, "failed")
        await message.reply_text(
            "Failed to process track. Try again or run /status for current state.\n"
            f"Error: {exc}"
        )


def run_bot() -> None:
    application = Application.builder().token(settings.telegram_bot_token).concurrent_updates(True).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("history", history_cmd))
    application.add_handler(CommandHandler("last", last_cmd))
    application.add_handler(CommandHandler("status", status_cmd))
    application.add_handler(CommandHandler("notes", notes_cmd))

    audio_or_audio_doc_or_text = (
        filters.AUDIO
        | filters.Document.MimeType("audio/")
        | (filters.TEXT & ~filters.COMMAND)
    )
    application.add_handler(MessageHandler(audio_or_audio_doc_or_text, handle_message))

    logger.info("Bot starting polling")
    application.run_polling(allowed_updates=Update.ALL_TYPES)
