from __future__ import annotations

import base64
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw
from openai import OpenAI

from config.settings import settings
from suno.metadata import TrackMetadata
from utils import get_logger

logger = get_logger(__name__)


def _safe_stem(value: str) -> str:
	cleaned = "".join(ch if ch.isalnum() or ch in ("_", "-", ".") else "_" for ch in value.strip())
	return (cleaned or "cover")[:80]


def _prompt_from_meta(meta: TrackMetadata) -> str:
	lyrics_snippet = (meta.lyrics or "").strip().replace("\n", " ")
	if len(lyrics_snippet) > 180:
		lyrics_snippet = f"{lyrics_snippet[:180]}..."
	parts = [
		"Create premium album art for music streaming platforms.",
		f"Track title: {meta.title}.",
		f"Artist: {meta.artist}.",
		f"Genre/style: {meta.genre}.",
	]
	if meta.style:
		parts.append(f"Suno style tags: {meta.style}.")
	if meta.prompt:
		parts.append(f"Creative direction from song prompt: {meta.prompt}.")
	if lyrics_snippet:
		parts.append(f"Lyrical mood hint: {lyrics_snippet}.")
	parts.append("Requirements: no text, no logos, cinematic lighting, highly detailed, square composition, modern album cover.")
	return " ".join(parts)


def _finalize_cover(img: Image.Image, target: Path) -> Path:
	final_img = img.convert("RGB").resize((3000, 3000), Image.Resampling.LANCZOS)
	final_img.save(target, format="JPEG", quality=95, optimize=True, progressive=True)
	return target


def _save_placeholder(meta: TrackMetadata) -> Path:
	target = settings.paths.cover_dir / f"{_safe_stem(meta.title)}_cover.jpg"
	img = Image.new("RGB", (3000, 3000), color=(22, 22, 30))
	draw = ImageDraw.Draw(img)
	draw.text((120, 1450), meta.title[:40], fill=(200, 200, 210))
	return _finalize_cover(img, target)


async def generate_cover(meta: TrackMetadata) -> Path:
	provider = (settings.image_provider or "grok").strip().lower()
	if provider == "grok":
		api_key = settings.grok_api_key
	elif provider == "openai":
		api_key = settings.openai_api_key
	else:
		api_key = settings.grok_api_key or settings.openai_api_key

	# If no key, immediately use placeholder
	if not api_key:
		return _save_placeholder(meta)

	try:
		if provider == "grok":
			client = OpenAI(api_key=api_key, base_url=settings.grok_base_url)
		elif provider == "openai":
			client = OpenAI(api_key=api_key)
		else:
			client = OpenAI(api_key=api_key, base_url=settings.grok_base_url)
		prompt = _prompt_from_meta(meta)

		result = client.images.generate(
			model=settings.image_model,
			prompt=prompt,
			size="1024x1024",
			n=1,
		)
		b64 = result.data[0].b64_json

		data = base64.b64decode(b64)
		img = Image.open(BytesIO(data)).convert("RGB")
		target = settings.paths.cover_dir / f"{_safe_stem(meta.title)}_cover.jpg"
		return _finalize_cover(img, target)
	except Exception as exc:
		logger.warning("Image generation failed for provider '%s': %s", provider, exc)
		return _save_placeholder(meta)
