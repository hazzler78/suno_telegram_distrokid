from __future__ import annotations

from pathlib import Path
from typing import Any
import json
import re

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

from config.settings import settings
from suno.metadata import TrackMetadata
from utils import get_logger

logger = get_logger(__name__)


DISTROKID_URL = "https://distrokid.com/"


async def upload_release(audio_path: Path, cover_path: Path, metadata: TrackMetadata) -> bool:
	if not settings.distrokid_email or not settings.distrokid_password:
		logger.warning("DistroKid credentials missing; skipping upload step.")
		return False

	def _snapshot(page, name: str) -> None:
		try:
			# Note: this inner function is sync; we schedule screenshots via page.screenshot.await is not allowed here
			pass
		except Exception:
			pass

	cookies_path = settings.paths.cookies_dir / "distrokid_cookies.json"
	
	async with async_playwright() as p:
		browser = await p.chromium.launch(headless=not settings.debug)
		context = await browser.new_context(accept_downloads=False)
		
		# Load existing cookies if available
		if cookies_path.exists():
			try:
				cookies = json.loads(cookies_path.read_text())
				await context.add_cookies(cookies)
				logger.info("Loaded DistroKid cookies from %s", cookies_path)
			except Exception as exc:
				logger.warning("Failed to load DistroKid cookies: %s", exc)
		
		if settings.debug:
			await context.tracing.start(screenshots=True, snapshots=True, sources=False)
		page = await context.new_page()

		try:
			# Go directly to the new release page. If not logged in, DistroKid will redirect
			# to a login/signup page. Let the human complete login once, then reuse cookies.
			await page.goto("https://distrokid.com/newrelease/", wait_until="domcontentloaded")

			if re.search(r"/login", page.url, re.IGNORECASE):
				logger.info(
					"DistroKid requires login. Please accept cookies and sign in in the opened browser; "
					"waiting up to 3 minutes for /newrelease/ to load..."
				)
				try:
					await page.wait_for_url(
						re.compile(r".*distrokid\.com/.*/newrelease.*", re.IGNORECASE),
						timeout=180_000,
					)
				except TimeoutError as te:
					logger.warning("DistroKid login did not complete in time: %s", te)
					ss_path = settings.paths.work_dir / "distrokid_login_timeout.png"
					try:
						await page.screenshot(path=str(ss_path), full_page=True)
						logger.info("Saved DistroKid login-timeout screenshot to %s", ss_path)
					except Exception:
						pass
					return False

			await page.wait_for_load_state("networkidle")
			await page.screenshot(path=str(settings.paths.work_dir / "distrokid_newrelease.png"))
			await page.screenshot(path=str(settings.paths.work_dir / "distrokid_newrelease.png"))

			cover_inputs = ["input[type=file][name=cover]", "input[type=file][accept*='image']"]
			for sel in cover_inputs:
				if await page.locator(sel).count() > 0:
					await page.set_input_files(sel, str(cover_path))
					break

			audio_inputs = ["input[type=file][name=audio]", "input[type=file][accept*='audio']"]
			for sel in audio_inputs:
				if await page.locator(sel).count() > 0:
					await page.set_input_files(sel, str(audio_path))
					break

			await page.locator("input[name=title], input#title").first.fill(metadata.title)
			await page.locator("input[name=artist], input#artist").first.fill(metadata.artist)
			if await page.locator("input[name=genre]").count() > 0:
				await page.fill("input[name=genre]", metadata.genre)

			await page.screenshot(path=str(settings.paths.work_dir / "distrokid_filled.png"))

			submit_selectors = ["button[type=submit]", "button:has-text('Done')", "button:has-text('Submit')"]
			for sel in submit_selectors:
				if await page.locator(sel).count() > 0:
					await page.locator(sel).first.click()
					break

			try:
				await page.wait_for_load_state("networkidle")
				url = page.url
				text_ok = (
					await page.locator("text=/release submitted|thanks|success/i").count()
					> 0
				)
				if "/done" in url or text_ok:
					logger.info("DistroKid submission appears successful: %s", url)
					return True
			except PlaywrightTimeoutError:
				pass

			ss_path = settings.paths.work_dir / "distrokid_submit_failed.png"
			await page.screenshot(path=str(ss_path), full_page=True)
			logger.warning("DistroKid submission may have failed; screenshot saved at %s", ss_path)
			return False
		except PlaywrightTimeoutError as te:
			logger.warning("DistroKid flow timeout: %s", te)
			return False
		except Exception as exc:
			logger.exception("DistroKid upload flow failed: %s", exc)
			return False
		finally:
			# Save cookies for next time
			try:
				cookies = await context.cookies()
				cookies_path.write_text(json.dumps(cookies))
				logger.info("Saved DistroKid cookies to %s", cookies_path)
			except Exception as exc:
				logger.warning("Failed to save DistroKid cookies: %s", exc)
			
			if settings.debug:
				try:
					await context.tracing.stop(path=str(settings.paths.work_dir / "trace_distrokid.zip"))
					logger.info("Saved DistroKid trace: %s", settings.paths.work_dir / "trace_distrokid.zip")
				except Exception:
					pass
			await context.close()
			await browser.close()
