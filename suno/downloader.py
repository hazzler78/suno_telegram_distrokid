from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

import aiohttp
from telegram import Message
from telegram.ext import ContextTypes

from config.settings import settings
from utils import get_logger
from .scraper import download_suno_song_via_browser

logger = get_logger(__name__)

SUNO_LINK_RE = re.compile(r"https?://(?:www\.)?suno\.(?:ai|com)/[^\s]+", re.IGNORECASE)
UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
AUDIO_URL_RE = re.compile(r"https?://[^\s\"']+\.(?:mp3|wav)(?:\?[^\s\"']*)?", re.IGNORECASE)
EMBEDDED_AUDIO_RE = re.compile(r'"audio_url"\s*:\s*"(https?://[^\"]+)"', re.IGNORECASE)


@dataclass
class DownloadResult:
    audio_path: Path
    source_url: str
    song_id: Optional[str] = None
    source_metadata: dict[str, Any] = field(default_factory=dict)


async def _download_from_telegram(message: Message, context: ContextTypes.DEFAULT_TYPE) -> Optional[DownloadResult]:
    file = None
    if message.audio:
        file = await message.audio.get_file()
    elif message.document and message.document.mime_type and message.document.mime_type.startswith("audio/"):
        file = await message.document.get_file()
    if not file:
        return None

    filename = message.audio.file_name if message.audio else message.document.file_name
    if not filename:
        filename = "telegram_audio.wav"
    target = settings.paths.download_dir / filename
    await file.download_to_drive(custom_path=str(target))
    return DownloadResult(audio_path=target, source_url="telegram://upload")


def _extract_suno_url(text: str) -> Optional[str]:
    match = SUNO_LINK_RE.search(text)
    return match.group(0) if match else None


def _extract_uuid(url: str) -> Optional[str]:
    direct = UUID_RE.search(url)
    if direct:
        return direct.group(0)
    parsed = urlparse(url)
    for values in parse_qs(parsed.query).values():
        for value in values:
            m = UUID_RE.search(value)
            if m:
                return m.group(0)
    return None


def _normalize_suno_url(url: str, uuid: Optional[str]) -> str:
    if uuid:
        return f"https://suno.com/song/{uuid}"
    return url


async def _http_get_text(session: aiohttp.ClientSession, url: str) -> str:
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
        resp.raise_for_status()
        return await resp.text(errors="ignore")


async def _download_audio_url(session: aiohttp.ClientSession, audio_url: str, song_id: Optional[str]) -> Optional[Path]:
    try:
        async with session.get(audio_url, timeout=aiohttp.ClientTimeout(total=120)) as resp:
            if resp.status >= 400:
                return None
            content_type = resp.headers.get("content-type", "").lower()
            if "audio" not in content_type and not audio_url.lower().endswith((".mp3", ".wav")):
                return None
            data = await resp.read()
            if not data:
                return None
            suffix = ".wav" if ".wav" in audio_url.lower() else ".mp3"
            stem = song_id or "suno_track"
            target = settings.paths.download_dir / f"{stem}{suffix}"
            target.write_bytes(data)
            return target
    except Exception:
        return None


def _extract_html_metadata(html_text: str) -> dict[str, Any]:
    unescaped = html.unescape(html_text).replace("\\u0026", "&").replace("\\/", "/")
    meta: dict[str, Any] = {}
    title_match = re.search(r'"title"\s*:\s*"([^"]+)"', unescaped)
    lyrics_match = re.search(r'"lyrics"\s*:\s*"([^"]+)"', unescaped)
    tags_match = re.search(r'"tags"\s*:\s*"([^"]+)"', unescaped)
    prompt_match = re.search(r'"prompt"\s*:\s*"([^"]+)"', unescaped)
    creator_match = re.search(r'"display_name"\s*:\s*"([^"]+)"', unescaped)
    if title_match:
        meta["title"] = title_match.group(1)
    if creator_match:
        meta["artist"] = creator_match.group(1)
    if lyrics_match:
        meta["lyrics"] = lyrics_match.group(1).replace("\\n", "\n")
    if tags_match:
        meta["style"] = tags_match.group(1)
    if prompt_match:
        meta["prompt"] = prompt_match.group(1)
    return meta


async def _download_from_suno_link(url: str) -> DownloadResult:
    song_id = _extract_uuid(url)
    normalized_url = _normalize_suno_url(url, song_id)
    source_meta: dict[str, Any] = {}

    async with aiohttp.ClientSession(headers={"User-Agent": "Mozilla/5.0"}) as session:
        # 1) Most reliable fast path: CDN by UUID
        if song_id:
            for ext in ("mp3", "wav"):
                cdn_url = f"https://cdn1.suno.ai/{song_id}.{ext}"
                path = await _download_audio_url(session, cdn_url, song_id=song_id)
                if path:
                    logger.info("Downloaded Suno track via CDN: %s", cdn_url)
                    return DownloadResult(audio_path=path, source_url=normalized_url, song_id=song_id)

        # 2) Try extracting metadata + embedded audio from page HTML
        try:
            html_text = await _http_get_text(session, normalized_url)
            source_meta.update(_extract_html_metadata(html_text))
            unescaped = html.unescape(html_text)
            audio_match = EMBEDDED_AUDIO_RE.search(unescaped) or AUDIO_URL_RE.search(unescaped)
            if audio_match:
                audio_url = audio_match.group(1) if audio_match.lastindex else audio_match.group(0)
                audio_url = audio_url.replace("\\u0026", "&").replace("\\/", "/")
                path = await _download_audio_url(session, audio_url, song_id=song_id)
                if path:
                    logger.info("Downloaded Suno track via page-extracted audio URL")
                    return DownloadResult(
                        audio_path=path,
                        source_url=normalized_url,
                        song_id=song_id,
                        source_metadata=source_meta,
                    )
        except Exception as exc:
            logger.info("HTTP fetch path failed for Suno link %s: %s", normalized_url, exc)

    # 3) Playwright fallback for JS-heavy/private/library songs
    browser_result = await download_suno_song_via_browser(normalized_url, settings.paths.download_dir)
    if browser_result and browser_result.audio_path:
        merged_meta = source_meta | browser_result.metadata
        return DownloadResult(
            audio_path=browser_result.audio_path,
            source_url=normalized_url,
            song_id=song_id or browser_result.song_id,
            source_metadata=merged_meta,
        )

    raise RuntimeError("Could not locate/download audio from Suno link. Private songs may require cookies/login.")


async def download_from_message(message: Message, context: ContextTypes.DEFAULT_TYPE) -> DownloadResult:
    if message.text:
        url = _extract_suno_url(message.text)
        if url:
            return await _download_from_suno_link(url)

    upload_result = await _download_from_telegram(message, context)
    if upload_result:
        return upload_result

    raise RuntimeError("No supported audio file or Suno URL found in message.")
