from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Optional
from urllib.error import URLError, HTTPError
from urllib.request import Request, urlopen

from mutagen import File as MutagenFile

from config.settings import settings
from utils import get_logger

logger = get_logger(__name__)


@dataclass
class TrackMetadata:
    title: str
    artist: str
    genre: str
    lyrics: Optional[str] = None
    style: Optional[str] = None
    prompt: Optional[str] = None
    source_url: Optional[str] = None
    song_id: Optional[str] = None


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
        prompt = (
            "Guess a single-word genre given the song title and optional lyrics. "
            "Respond with only the genre word.\n"
            f"Title: {title}\n"
            f"Lyrics: {lyrics or 'N/A'}\n"
        )
        body = {
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
            "max_tokens": 6,
        }
        req = Request(
            url="https://api.openai.com/v1/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {settings.openai_api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        guess = payload["choices"][0]["message"]["content"].strip()
        return guess.split("\n")[0][:40]
    except (HTTPError, URLError, KeyError, ValueError) as exc:
        logger.warning("OpenAI genre guess failed: %s", exc)
        return "Electronic"
    except Exception as exc:
        logger.warning("OpenAI genre guess failed: %s", exc)
        return "Electronic"


async def parse_metadata_with_ai_fallback(path: Path, source_metadata: Optional[dict[str, Any]] = None) -> TrackMetadata:
    source_metadata = source_metadata or {}
    tags = _read_basic_tags(path)
    title = source_metadata.get("title") or tags.get("title") or tags.get("tit2") or path.stem
    artist = source_metadata.get("artist") or tags.get("artist") or tags.get("tpe1") or "Unknown Artist"
    genre = source_metadata.get("genre") or source_metadata.get("style") or tags.get("genre") or tags.get("tcon")
    lyrics = source_metadata.get("lyrics") or tags.get("uslt")
    style = source_metadata.get("style")
    prompt = source_metadata.get("prompt")

    if not genre:
        genre = _ai_guess_genre(title, lyrics)

    return TrackMetadata(
        title=title,
        artist=artist,
        genre=genre,
        lyrics=lyrics,
        style=style,
        prompt=prompt,
        source_url=source_metadata.get("source_url"),
        song_id=source_metadata.get("song_id"),
    )
