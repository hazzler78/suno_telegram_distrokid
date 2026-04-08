from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw
from openai import OpenAI

from config.settings import settings
from suno.metadata import TrackMetadata
from utils import get_logger

logger = get_logger(__name__)


def _prompt_from_meta(meta: TrackMetadata) -> str:
	return (
		f"Album cover for {meta.title}, {meta.genre} vibes, cinematic, artistic, modern, "
		f"minimal text, square composition, high detail, digital art"
	)


def _save_placeholder(meta: TrackMetadata) -> Path:
	target = settings.paths.cover_dir / f"{meta.title}_cover.png"
	img = Image.new("RGB", (3000, 3000), color=(22, 22, 30))
	draw = ImageDraw.Draw(img)
	draw.text((120, 1450), meta.title[:40], fill=(200, 200, 210))
	img.save(target, format="PNG")
	return target


async def generate_cover(meta: TrackMetadata) -> Path:
	# If no key, immediately use placeholder
	if not settings.openai_api_key:
		return _save_placeholder(meta)

	try:
		client = OpenAI()  # rely on OPENAI_API_KEY env var
		prompt = _prompt_from_meta(meta)

		result = client.images.generate(
			model="gpt-image-1",
			prompt=prompt,
			size="1024x1024",
			n=1,
		)
		b64 = result.data[0].b64_json
		import base64

		data = base64.b64decode(b64)
		img = Image.open(BytesIO(data)).convert("RGB")
		img = img.resize((3000, 3000))
		target = settings.paths.cover_dir / f"{meta.title}_cover.png"
		img.save(target, format="PNG")
		return target
	except Exception as exc:
		logger.warning("OpenAI image generation failed: %s", exc)
		return _save_placeholder(meta)
