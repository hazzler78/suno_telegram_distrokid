from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from mutagen import File as MutagenFile
from openai import OpenAI

from config.settings import settings
from utils import get_logger

logger = get_logger(__name__)


@dataclass
class TrackMetadata:
    title: str
    artist: str
    genre: str
    lyrics: Optional[str] = None


def _read_basic_tags(path: Path) -> dict[str, str]:
    audio = MutagenFile(path)
    tags: dict[str, str] = {}
    if not audio or not getattr(audio, "tags", None):
        return tags
    for key in ("title", "artist", "TIT2", "TPE1", "TCON", "genre", "USLT"):
        if key in audio.tags:
            try:
                value = audio.tags.get(key)
                if isinstance(value, list):
                    value = value[0]
                if hasattr(value, "text"):
                    value = ", ".join(getattr(value, "text"))
                if value:
                    tags[key.lower()] = str(value)
            except Exception:
                continue
    return tags


def _ai_guess_genre(title: str, lyrics: Optional[str]) -> str:
    if not settings.openai_api_key:
        return "Electronic"
    try:
        # Use env var implicitly; avoid passing unexpected kwargs
        client = OpenAI()
        prompt = (
            "Guess a single-word genre given the song title and optional lyrics. "
            "Respond with only the genre word.\n"
            f"Title: {title}\n"
            f"Lyrics: {lyrics or 'N/A'}\n"
        )
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=6,
        )
        guess = response.choices[0].message.content.strip()
        return guess.split("\n")[0][:40]
    except Exception as exc:
        logger.warning("OpenAI genre guess failed: %s", exc)
        return "Electronic"


async def parse_metadata_with_ai_fallback(path: Path) -> TrackMetadata:
    tags = _read_basic_tags(path)
    title = tags.get("title") or tags.get("tit2") or path.stem
    artist = tags.get("artist") or tags.get("tpe1") or "Unknown Artist"
    genre = tags.get("genre") or tags.get("tcon")
    lyrics = tags.get("uslt")

    if not genre:
        genre = _ai_guess_genre(title, lyrics)

    return TrackMetadata(title=title, artist=artist, genre=genre, lyrics=lyrics)
