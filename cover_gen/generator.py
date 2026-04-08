from __future__ import annotations

import base64
import hashlib
import random
from io import BytesIO
from pathlib import Path
from typing import Any

import aiohttp
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
	seed_source = f"{meta.title}|{meta.artist}|{meta.genre}"
	seed = int(hashlib.sha256(seed_source.encode("utf-8")).hexdigest()[:8], 16)
	rng = random.Random(seed)
	img = Image.new("RGB", (3000, 3000), color=(rng.randint(10, 40), rng.randint(10, 40), rng.randint(20, 60)))
	draw = ImageDraw.Draw(img)

	# Procedural fallback art: layered translucent circles for non-black, no-text cover.
	for _ in range(24):
		x = rng.randint(-600, 2600)
		y = rng.randint(-600, 2600)
		r = rng.randint(300, 1200)
		color = (
			rng.randint(40, 240),
			rng.randint(40, 240),
			rng.randint(40, 240),
			rng.randint(70, 130),
		)
		draw.ellipse((x, y, x + r, y + r), fill=color)
	return _finalize_cover(img, target)


def _extract_image_bytes(payload: dict[str, Any]) -> bytes:
	data = payload.get("data") or []
	if not data:
		raise ValueError("No image entries returned by provider.")
	first = data[0] or {}
	b64 = first.get("b64_json")
	if b64:
		return base64.b64decode(b64)
	url = first.get("url")
	if url:
		raise ValueError(f"Provider returned URL image response: {url}")
	raise ValueError("Unsupported image response format (missing b64_json/url).")


async def _generate_image_http(
	*, base_url: str, api_key: str, model: str, prompt: str, provider: str
) -> bytes:
	endpoint = f"{base_url.rstrip('/')}/images/generations"
	payload: dict[str, Any] = {
		"model": model,
		"prompt": prompt,
		"n": 1,
	}
	# Provider-specific compatibility:
	# xAI currently rejects "size" in this endpoint for selected models.
	if provider != "grok":
		payload["size"] = "1024x1024"
		# gpt-image-1 rejects response_format in this endpoint in your environment.
		# Keep request minimal and parse whichever image format is returned.
	headers = {
		"Authorization": f"Bearer {api_key}",
		"Content-Type": "application/json",
	}
	timeout = aiohttp.ClientTimeout(total=90)
	async with aiohttp.ClientSession(timeout=timeout) as session:
		async with session.post(endpoint, json=payload, headers=headers) as resp:
			body_text = await resp.text()
			if resp.status >= 400:
				raise RuntimeError(f"Image API error {resp.status}: {body_text[:500]}")
			try:
				body_json = await resp.json(content_type=None)
			except Exception:
				raise RuntimeError(f"Image API returned non-JSON payload: {body_text[:500]}")
	data = body_json.get("data") or []
	if not data:
		raise RuntimeError(f"Image API returned no data: {str(body_json)[:500]}")
	first = data[0] or {}
	b64 = first.get("b64_json")
	if b64:
		return base64.b64decode(b64)
	url = first.get("url")
	if url:
		# Some providers return URL responses instead of inline base64.
		async with aiohttp.ClientSession(timeout=timeout) as session:
			async with session.get(url) as img_resp:
				img_resp.raise_for_status()
				return await img_resp.read()
	return _extract_image_bytes(body_json)


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

	prompt = _prompt_from_meta(meta)
	target = settings.paths.cover_dir / f"{_safe_stem(meta.title)}_cover.jpg"

	# Prefer raw HTTP path first (more stable in this environment than SDK clients).
	try:
		if provider == "grok":
			image_bytes = await _generate_image_http(
				base_url=settings.grok_base_url,
				api_key=api_key,
				model=settings.image_model,
				prompt=prompt,
				provider=provider,
			)
		else:
			image_bytes = await _generate_image_http(
				base_url="https://api.openai.com/v1",
				api_key=api_key,
				model=settings.image_model,
				prompt=prompt,
				provider=provider,
			)
		img = Image.open(BytesIO(image_bytes)).convert("RGB")
		return _finalize_cover(img, target)
	except Exception as http_exc:
		logger.warning("HTTP image generation failed for provider '%s': %s", provider, http_exc)
		# Auto-fallback: if Grok model is unavailable, try OpenAI image generation.
		if provider == "grok" and settings.openai_api_key:
			try:
				logger.info(
					"Falling back to OpenAI image generation model '%s' due to Grok failure.",
					settings.openai_image_model,
				)
				image_bytes = await _generate_image_http(
					base_url="https://api.openai.com/v1",
					api_key=settings.openai_api_key,
					model=settings.openai_image_model,
					prompt=prompt,
					provider="openai",
				)
				img = Image.open(BytesIO(image_bytes)).convert("RGB")
				return _finalize_cover(img, target)
			except Exception as openai_fallback_exc:
				logger.warning("OpenAI fallback image generation failed: %s", openai_fallback_exc)

	# Last attempt with SDK (kept as fallback only).
	try:
		if provider == "grok":
			client = OpenAI(api_key=api_key, base_url=settings.grok_base_url)
		elif provider == "openai":
			client = OpenAI(api_key=api_key)
		else:
			client = OpenAI(api_key=api_key, base_url=settings.grok_base_url)
		result = client.images.generate(
			model=settings.image_model,
			prompt=prompt,
			size="1024x1024",
			n=1,
			response_format="b64_json",
		)
		b64 = result.data[0].b64_json
		data = base64.b64decode(b64)
		img = Image.open(BytesIO(data)).convert("RGB")
		return _finalize_cover(img, target)
	except Exception as exc:
		logger.warning("SDK image generation failed for provider '%s': %s", provider, exc)
		return _save_placeholder(meta)
