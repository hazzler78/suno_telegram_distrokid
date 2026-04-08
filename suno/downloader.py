from __future__ import annotations

import asyncio
import html
import re
from pathlib import Path
from typing import Optional

import aiohttp
from telegram import Message
from telegram.ext import ContextTypes

from config.settings import settings
from utils import get_logger
from .scraper import download_suno_song_via_browser

logger = get_logger(__name__)

# Support both suno.ai and suno.com links
SUNO_URL_RE = re.compile(
	r"https?://(?:www\.)?suno\.(?:ai|com)/(?:song|track|listen)/[\w-]+",
	re.IGNORECASE,
)

# Extract direct audio URLs from HTML/JSON blobs
AUDIO_URL_RE = re.compile(r"https?://[^\s\"']+\.(?:mp3|wav)(?:\?[^\s\"']*)?", re.IGNORECASE)
EMBEDDED_AUDIO_RE = re.compile(r'"audio_url"\s*:\s*"(https?://[^\"]+)"', re.IGNORECASE)


async def _download_from_telegram(message: Message, context: ContextTypes.DEFAULT_TYPE) -> Optional[Path]:
	file = None
	if message.audio:
		file = await message.audio.get_file()
	elif message.document and message.document.mime_type and message.document.mime_type.startswith("audio/"):
		file = await message.document.get_file()
	if not file:
		return None

	filename = message.audio.file_name if message.audio else message.document.file_name
	if not filename:
		filename = "suno_track.wav"

	target = settings.paths.download_dir / filename
	await file.download_to_drive(custom_path=str(target))
	return target


async def _http_get_text(session: aiohttp.ClientSession, url: str) -> str:
	async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
		resp.raise_for_status()
		return await resp.text(errors="ignore")


async def _http_get_bytes(session: aiohttp.ClientSession, url: str) -> bytes:
	async with session.get(url, timeout=aiohttp.ClientTimeout(total=120)) as resp:
		resp.raise_for_status()
		return await resp.read()


async def _download_from_suno_link(url: str) -> Path:
	target = settings.paths.download_dir / "suno_track.mp3"

	async with aiohttp.ClientSession(headers={"User-Agent": "Mozilla/5.0"}) as session:
		# 1) If the URL itself is a direct audio link, download it
		try:
			async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
				content_type = resp.headers.get("content-type", "").lower()
				if "audio" in content_type:
					data = await resp.read()
					target.write_bytes(data)
					return target
		except Exception:
			pass

		# 2) Otherwise, fetch HTML and search for an audio URL
		html_text = await _http_get_text(session, url)
		# Unescape any escaped entities
		html_text_unescaped = html.unescape(html_text)

		# Try embedded JSON key first
		m = EMBEDDED_AUDIO_RE.search(html_text_unescaped)
		audio_url: Optional[str] = None
		if m:
			audio_url = m.group(1)
		else:
			# Fallback: any .mp3 or .wav in the page
			m2 = AUDIO_URL_RE.search(html_text_unescaped)
			if m2:
				audio_url = m2.group(0)

		if audio_url:
			# Some pages escape characters like \u0026, fix them
			audio_url = audio_url.replace("\\u0026", "&").replace("\\/", "/")

			# Download the found audio URL
			data = await _http_get_bytes(session, audio_url)
			# Choose extension based on URL
			suffix = ".wav" if audio_url.lower().endswith(".wav") else ".mp3"
			final_target = settings.paths.download_dir / f"suno_track{suffix}"
			final_target.write_bytes(data)
			return final_target

	# 3) Fallback: login and attempt download via browser automation
	browser_path = await download_suno_song_via_browser(url, settings.paths.download_dir)
	if browser_path:
		return browser_path

	raise RuntimeError("Could not locate or download audio from Suno page. Login may be required.")


async def download_from_message(message: Message, context: ContextTypes.DEFAULT_TYPE) -> Path:
	if message.text:
		match = SUNO_URL_RE.search(message.text)
		if match:
			return await _download_from_suno_link(match.group(0))

	path = await _download_from_telegram(message, context)
	if path:
		return path

	raise RuntimeError("No supported audio or Suno link found in message")
