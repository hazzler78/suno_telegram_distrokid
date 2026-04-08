from __future__ import annotations

import asyncio
from typing import Optional

from utils import get_logger

logger = get_logger(__name__)


async def is_human_verification_present(page) -> bool:
	try:
		# Look for common captcha iframes or text markers
		iframe_matches = await page.locator(
			"iframe[src*='captcha'], iframe[src*='hcaptcha'], iframe[src*='recaptcha'], iframe[src*='cloudflare']"
		).count()
		text_matches = await page.locator(
			"text=/verify you are human|are you a human|recaptcha|hcaptcha|just a moment|checking your browser/i"
		).count()
		return (iframe_matches + text_matches) > 0
	except Exception:
		return False


async def wait_for_human_verification(page, timeout_seconds: int = 180) -> bool:
	"""Wait until human verification disappears, polling periodically.
	Returns True if cleared, False if still present after timeout.
	"""
	time_left = timeout_seconds
	while time_left > 0:
		if not await is_human_verification_present(page):
			return True
		await asyncio.sleep(2)
		time_left -= 2
	return not await is_human_verification_present(page)
