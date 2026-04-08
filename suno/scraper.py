from __future__ import annotations

from pathlib import Path
from typing import Optional

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

from config.settings import settings
from utils import get_logger
from utils.human_verification import is_human_verification_present, wait_for_human_verification

logger = get_logger(__name__)


async def _snapshot(page, name: str) -> None:
	try:
		ss = settings.paths.work_dir / f"suno_{name}.png"
		html_path = settings.paths.work_dir / f"suno_{name}.html"
		await page.screenshot(path=str(ss), full_page=True)
		content = await page.content()
		html_path.write_text(content, encoding="utf-8")
		logger.info("Saved Suno snapshot: %s | %s", ss, html_path)
	except Exception:
		pass


async def _handle_human_verification(page) -> bool:
	if await is_human_verification_present(page):
		await _snapshot(page, "human_verification")
		if settings.debug:
			logger.info("Human verification detected. Please solve it in the visible browser window.")
			cleared = await wait_for_human_verification(page)
			if cleared:
				logger.info("Human verification cleared.")
				return True
			return False
		else:
			logger.warning("Human verification detected (headless). Enable DEBUG=1 to solve manually.")
			return False
	return True


async def _login_via_suno_credentials(page) -> bool:
	if not settings.suno_email or not settings.suno_password:
		return False
	await page.goto("https://suno.com/login")
	await _snapshot(page, "login_page")
	if not await _handle_human_verification(page):
		return False
	await page.fill("input[type=email]", settings.suno_email)
	await page.fill("input[type=password]", settings.suno_password)
	await _snapshot(page, "login_filled")
	await page.click("button:has-text('Log in'), button:has-text('Sign in')")
	await page.wait_for_load_state("networkidle")
	await _snapshot(page, "login_after")
	return True


async def _login_via_discord(page, context) -> bool:
	if not settings.discord_email or not settings.discord_password:
		return False
	await page.goto("https://suno.com/login")
	await _snapshot(page, "discord_button")
	if not await _handle_human_verification(page):
		return False
	discord_btn = page.get_by_role("button", name=lambda n: n and "Discord" in n)
	if await discord_btn.count() == 0:
		candidates = ["button:has-text('Discord')", "a:has-text('Discord')"]
		for sel in candidates:
			if await page.locator(sel).count() > 0:
				discord_btn = page.locator(sel)
				break
	await _snapshot(page, "discord_before_click")

	async with context.expect_page() as popup_info:
		await discord_btn.first.click()
	discord = await popup_info.value
	await discord.wait_for_load_state("domcontentloaded")
	await _snapshot(discord, "discord_login_page")

	await discord.fill("input[name=email]", settings.discord_email)
	await discord.fill("input[name=password]", settings.discord_password)
	await _snapshot(discord, "discord_filled")
	await discord.click("button[type=submit]")
	try:
		await discord.click("button:has-text('Authorize')", timeout=10000)
	except Exception:
		pass
	await discord.wait_for_load_state("networkidle")
	await page.wait_for_load_state("networkidle")
	await _snapshot(page, "discord_after")
	return True


async def _ensure_logged_in(context, page) -> None:
	logged_in = False
	if settings.discord_email and settings.discord_password:
		try:
			logged_in = await _login_via_discord(page, context)
		except Exception as exc:
			logger.warning("Discord login flow failed: %s", exc)
	if not logged_in and settings.suno_email and settings.suno_password:
		try:
			logged_in = await _login_via_suno_credentials(page)
		except Exception as exc:
			logger.warning("Suno credential login failed: %s", exc)


async def download_suno_song_via_browser(song_url: str, target_dir: Path) -> Optional[Path]:
	trace_path = settings.paths.work_dir / "trace_suno.zip"
	cookies_path = settings.paths.cookies_dir / "suno_cookies.json"
	
	async with async_playwright() as p:
		browser = await p.chromium.launch(headless=not settings.debug)
		context = await browser.new_context(accept_downloads=True)
		
		# Load existing cookies if available
		if cookies_path.exists():
			try:
				await context.add_cookies(cookies_path.read_text())
				logger.info("Loaded Suno cookies from %s", cookies_path)
			except Exception as exc:
				logger.warning("Failed to load Suno cookies: %s", exc)
		
		if settings.debug:
			await context.tracing.start(screenshots=True, snapshots=True, sources=False)
		page = await context.new_page()

		try:
			await _ensure_logged_in(context, page)
			await page.goto(song_url, wait_until="networkidle")
			await _snapshot(page, "song_page")
			if not await _handle_human_verification(page):
				return None

			selectors = ["button:has-text('Download')", "a:has-text('Download')", "[data-testid='download']"]
			for sel in selectors:
				if await page.locator(sel).count() > 0:
					break
			else:
				sel = None

			if not sel:
				logger.warning("No download button found on the Suno song page.")
				return None

			with page.expect_download(timeout=45000) as download_info:
				await page.locator(sel).first.click()
			download = await download_info.value

			filename = download.suggested_filename or "suno_track.mp3"
			save_path = target_dir / filename
			await download.save_as(str(save_path))
			logger.info("Suno download saved: %s", save_path)
			return save_path
		except PlaywrightTimeoutError:
			logger.warning("Download timed out on Suno page: %s", song_url)
		except Exception as exc:
			logger.exception("Suno browser download failed: %s", exc)
		finally:
			# Save cookies for next time
			try:
				cookies = await context.cookies()
				cookies_path.write_text(str(cookies))
				logger.info("Saved Suno cookies to %s", cookies_path)
			except Exception as exc:
				logger.warning("Failed to save Suno cookies: %s", exc)
			
			if settings.debug:
				try:
					await context.tracing.stop(path=str(trace_path))
					logger.info("Saved Suno trace: %s", trace_path)
				except Exception:
					pass
			try:
				await context.close()
				await browser.close()
			except Exception:
				pass
	return None
