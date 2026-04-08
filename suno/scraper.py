from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

from config.settings import settings
from utils import get_logger
from utils.human_verification import is_human_verification_present, wait_for_human_verification

logger = get_logger(__name__)


@dataclass
class BrowserDownloadResult:
	audio_path: Optional[Path]
	metadata: dict[str, Any] = field(default_factory=dict)
	song_id: Optional[str] = None


def _extract_page_metadata(html_text: str) -> dict[str, Any]:
	meta: dict[str, Any] = {}
	patterns = {
		"title": r'"title"\s*:\s*"([^"]+)"',
		"artist": r'"display_name"\s*:\s*"([^"]+)"',
		"lyrics": r'"lyrics"\s*:\s*"([^"]+)"',
		"style": r'"tags"\s*:\s*"([^"]+)"',
		"prompt": r'"prompt"\s*:\s*"([^"]+)"',
	}
	for key, pattern in patterns.items():
		match = re.search(pattern, html_text)
		if match:
			value = match.group(1).replace("\\n", "\n").replace("\\u0026", "&").replace("\\/", "/")
			meta[key] = value
	return meta


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


async def _accept_cookie_banner(page) -> None:
	selectors = [
		"button:has-text('Accept all')",
		"button:has-text('Accept')",
		"button:has-text('I agree')",
		"button:has-text('Allow all')",
		"[id*='cookie'] button:has-text('Accept')",
	]
	for sel in selectors:
		try:
			loc = page.locator(sel)
			if await loc.count() > 0 and await loc.first.is_visible():
				await loc.first.click(timeout=2000)
				logger.info("Accepted Suno cookie banner using selector: %s", sel)
				await page.wait_for_timeout(500)
				return
		except Exception:
			continue


async def _login_via_suno_credentials(page) -> bool:
	if not settings.suno_email or not settings.suno_password:
		return False
	await page.goto("https://suno.com/login")
	await _accept_cookie_banner(page)
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
	await _accept_cookie_banner(page)
	await _snapshot(page, "discord_button")
	if not await _handle_human_verification(page):
		return False
	discord_btn = page.get_by_role("button", name=re.compile("discord", re.IGNORECASE))
	if await discord_btn.count() == 0:
		candidates = [
			"button:has-text('Discord')",
			"a:has-text('Discord')",
			"[data-testid*='discord']",
			"[aria-label*='discord' i]",
		]
		for sel in candidates:
			if await page.locator(sel).count() > 0:
				discord_btn = page.locator(sel)
				break
	if await discord_btn.count() == 0:
		logger.warning("Discord login button not found on Suno login page.")
		await _snapshot(page, "discord_button_missing")
		return False
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


async def _click_overflow_menu_by_geometry(page) -> bool:
	"""
	Fallback for Suno's icon-only three-dots button near thumbs up/down controls.
	Finds small visible SVG-only buttons and prefers the rightmost candidate.
	"""
	try:
		handle = await page.evaluate_handle(
			"""
			() => {
				const isVisible = (el) => {
					if (!el) return false;
					const style = window.getComputedStyle(el);
					if (style.visibility === 'hidden' || style.display === 'none') return false;
					const r = el.getBoundingClientRect();
					return r.width > 8 && r.height > 8 && r.bottom > 0 && r.right > 0;
				};

				const buttons = Array.from(document.querySelectorAll('button'));
				const candidates = buttons
					.filter((b) => {
						if (!isVisible(b)) return false;
						const txt = (b.textContent || '').trim();
						const hasSvg = !!b.querySelector('svg');
						const r = b.getBoundingClientRect();
						// Typical icon button dimensions for player controls.
						const iconSized = r.width <= 64 && r.height <= 64;
						return hasSvg && txt.length <= 2 && iconSized;
					})
					.map((b) => ({ b, r: b.getBoundingClientRect() }))
					// Player controls tend to be in lower half.
					.filter((x) => x.r.top > window.innerHeight * 0.35)
					.sort((a, c) => c.r.right - a.r.right);

				return candidates.length ? candidates[0].b : null;
			}
			"""
		)
		el = handle.as_element()
		if el:
			await el.click(timeout=2500)
			logger.info("Opened Suno menu via geometry fallback (rightmost icon button).")
			await page.wait_for_timeout(800)
			return True
	except Exception:
		pass
	return False


async def _click_and_wait_for_download(page, locator, timeout_ms: int) -> Optional[Any]:
	waiter = asyncio.create_task(page.wait_for_event("download", timeout=timeout_ms))
	try:
		await locator.click()
		download = await waiter
		return download
	except Exception:
		if not waiter.done():
			waiter.cancel()
		try:
			await waiter
		except Exception:
			pass
		return None


async def _download_from_modal_button(page, target_dir: Path, timeout_ms: int = 10000) -> Optional[Path]:
	"""
	Suno often opens a "Download WAV Audio" modal where the real action
	is clicking "Download File". Wait briefly for that button and click it.
	"""
	modal_selectors = [
		"[role='dialog'] button:has-text('Download File')",
		"button:has-text('Download File')",
		"[role='dialog'] a:has-text('Download File')",
		"a:has-text('Download File')",
	]
	for sel in modal_selectors:
		try:
			loc = page.locator(sel).first
			await loc.wait_for(state="visible", timeout=timeout_ms)
			logger.info("Suno modal download button ready via selector: %s", sel)
			download = await _click_and_wait_for_download(page, loc, timeout_ms=45000)
			if not download:
				continue
			filename = download.suggested_filename or "suno_track.mp3"
			save_path = target_dir / filename
			await download.save_as(str(save_path))
			logger.info("Suno modal-flow download saved: %s", save_path)
			return save_path
		except Exception:
			continue
	return None


async def _download_via_menu_flow(page, target_dir: Path) -> Optional[Path]:
	# New Suno flow: open "..." menu, choose WAV/MP3, then confirm Download.
	menu_selectors = [
		"button[aria-label*='more' i]",
		"button[aria-label*='options' i]",
		"button[aria-label*='menu' i]",
		"button:has-text('...')",
		"button:has-text('⋯')",
		"button:has-text('…')",
		"[data-testid*='more']",
		"[data-testid*='menu']",
		"[aria-haspopup='menu']",
	]
	menu_opened = False
	for sel in menu_selectors:
		try:
			loc = page.locator(sel)
			if await loc.count() > 0 and await loc.first.is_visible():
				await loc.first.click()
				menu_opened = True
				logger.info("Opened Suno menu via selector: %s", sel)
				await page.wait_for_timeout(800)
				break
		except Exception:
			continue
	if not menu_opened:
		try:
			dots_btn = page.get_by_role("button", name=re.compile(r"more|options|menu|actions", re.IGNORECASE))
			if await dots_btn.count() > 0 and await dots_btn.first.is_visible():
				await dots_btn.first.click()
				menu_opened = True
				logger.info("Opened Suno menu via role/name fallback.")
				await page.wait_for_timeout(800)
		except Exception:
			pass
	if not menu_opened:
		menu_opened = await _click_overflow_menu_by_geometry(page)
	if not menu_opened:
		await _snapshot(page, "menu_not_found")
		return None

	# Some Suno UIs require hovering "Download" before selecting "WAV Audio".
	hover_download_selectors = [
		"[role='menuitem']:has-text('Download')",
		"button:has-text('Download')",
		"a:has-text('Download')",
	]
	audio_option_selectors = [
		"[role='menuitem']:has-text('WAV Audio')",
		"button:has-text('WAV Audio')",
		"a:has-text('WAV Audio')",
		"[role='menuitem']:has-text('WAV')",
		"button:has-text('WAV')",
		"a:has-text('WAV')",
		"[role='menuitem']:has-text('MP3 Audio')",
		"button:has-text('MP3 Audio')",
		"a:has-text('MP3 Audio')",
		"[role='menuitem']:has-text('MP3')",
		"button:has-text('MP3')",
		"a:has-text('MP3')",
	]
	for hover_sel in hover_download_selectors:
		try:
			hover_loc = page.locator(hover_sel)
			if await hover_loc.count() > 0 and await hover_loc.first.is_visible():
				await hover_loc.first.hover()
				logger.info("Hovered Suno download menu via selector: %s", hover_sel)
				await page.wait_for_timeout(350)
				for audio_sel in audio_option_selectors:
					try:
						audio_loc = page.locator(audio_sel)
						if await audio_loc.count() == 0 or not await audio_loc.first.is_visible():
							continue
						download = await _click_and_wait_for_download(page, audio_loc.first, timeout_ms=15000)
						if not download:
							# In many flows audio click opens a modal with "Download File".
							modal_path = await _download_from_modal_button(page, target_dir, timeout_ms=10000)
							if modal_path:
								return modal_path
							continue
						filename = download.suggested_filename or "suno_track.mp3"
						save_path = target_dir / filename
						await download.save_as(str(save_path))
						logger.info("Suno menu-flow download saved via hover path (%s -> %s): %s", hover_sel, audio_sel, save_path)
						return save_path
					except Exception:
						continue
		except Exception:
			continue

	quality_selectors = [
		"button:has-text('Download WAV')",
		"[role='menuitem']:has-text('Download WAV')",
		"button:has-text('WAV')",
		"a:has-text('WAV')",
		"[role='menuitem']:has-text('WAV')",
		"button:has-text('Download MP3')",
		"[role='menuitem']:has-text('Download MP3')",
		"button:has-text('MP3')",
		"a:has-text('MP3')",
		"[role='menuitem']:has-text('MP3')",
	]
	quality_clicked = False
	for sel in quality_selectors:
		try:
			loc = page.locator(sel)
			if await loc.count() > 0 and await loc.first.is_visible():
				download = await _click_and_wait_for_download(page, loc.first, timeout_ms=15000)
				if download:
					filename = download.suggested_filename or "suno_track.mp3"
					save_path = target_dir / filename
					await download.save_as(str(save_path))
					logger.info("Suno menu-flow download saved directly from quality click (%s): %s", sel, save_path)
					return save_path
				modal_path = await _download_from_modal_button(page, target_dir, timeout_ms=10000)
				if modal_path:
					return modal_path
				await loc.first.click()
				quality_clicked = True
				logger.info("Selected Suno quality option via selector: %s", sel)
				await page.wait_for_timeout(1000)
				break
		except Exception:
			continue
	if not quality_clicked:
		return None

	confirm_selectors = [
		"button:has-text('Download')",
		"a:has-text('Download')",
		"[role='menuitem']:has-text('Download')",
	]
	for sel in confirm_selectors:
		try:
			loc = page.locator(sel)
			if await loc.count() == 0 or not await loc.first.is_visible():
				continue
			download = await _click_and_wait_for_download(page, loc.first, timeout_ms=45000)
			if not download:
				continue
			filename = download.suggested_filename or "suno_track.mp3"
			save_path = target_dir / filename
			await download.save_as(str(save_path))
			logger.info("Suno menu-flow download saved: %s", save_path)
			return save_path
		except Exception:
			continue
	return None


async def download_suno_song_via_browser(song_url: str, target_dir: Path) -> BrowserDownloadResult:
	trace_path = settings.paths.work_dir / "trace_suno.zip"
	cookies_path = settings.paths.cookies_dir / "suno_cookies.json"
	storage_state_path = settings.paths.cookies_dir / "suno_storage_state.json"
	
	async with async_playwright() as p:
		browser = await p.chromium.launch(headless=not settings.debug)
		if storage_state_path.exists():
			context = await browser.new_context(
				accept_downloads=True,
				storage_state=str(storage_state_path),
			)
			logger.info("Loaded Suno storage state from %s", storage_state_path)
		else:
			context = await browser.new_context(accept_downloads=True)
		
		# Load existing cookies if available
		if cookies_path.exists():
			try:
				cookies = json.loads(cookies_path.read_text(encoding="utf-8"))
				await context.add_cookies(cookies)
				logger.info("Loaded Suno cookies from %s", cookies_path)
			except Exception as exc:
				logger.warning("Failed to load Suno cookies: %s", exc)
		
		if settings.debug:
			await context.tracing.start(screenshots=True, snapshots=True, sources=False)
		page = await context.new_page()

		try:
			# First attempt on the song page using existing session/cookies.
			try:
				await page.goto(song_url, wait_until="domcontentloaded", timeout=25000)
			except PlaywrightTimeoutError:
				# Suno frequently keeps long-lived network requests open; continue anyway.
				logger.warning("Suno page navigation timeout; continuing with partial page load: %s", song_url)
			try:
				await page.wait_for_timeout(1500)
			except Exception:
				pass
			await _accept_cookie_banner(page)
			await _snapshot(page, "song_page")
			if not await _handle_human_verification(page):
				return BrowserDownloadResult(audio_path=None)
			page_meta = _extract_page_metadata(await page.content())

			# First try multi-step menu flow (... -> WAV/MP3 -> Download)
			menu_flow_path = await _download_via_menu_flow(page, target_dir)
			if menu_flow_path:
				return BrowserDownloadResult(audio_path=menu_flow_path, metadata=page_meta)

			selectors = ["button:has-text('Download')", "a:has-text('Download')", "[data-testid='download']"]
			for sel in selectors:
				if await page.locator(sel).count() > 0:
					break
			else:
				sel = None

			if not sel:
				# If no download controls are visible, try explicit login and retry once.
				if (settings.discord_email and settings.discord_password) or (
					settings.suno_email and settings.suno_password
				):
					logger.info("Retrying Suno download after explicit login flow.")
					await _ensure_logged_in(context, page)
					try:
						await page.goto(song_url, wait_until="domcontentloaded", timeout=25000)
					except PlaywrightTimeoutError:
						logger.warning(
							"Suno page navigation timeout after login; continuing with partial page load: %s",
							song_url,
						)
					try:
						await page.wait_for_timeout(1500)
					except Exception:
						pass
					await _accept_cookie_banner(page)
					await _snapshot(page, "song_page_after_login")
					page_meta = _extract_page_metadata(await page.content())

					menu_flow_path = await _download_via_menu_flow(page, target_dir)
					if menu_flow_path:
						return BrowserDownloadResult(audio_path=menu_flow_path, metadata=page_meta)

					for sel in selectors:
						if await page.locator(sel).count() > 0:
							break
					else:
						sel = None

				if not sel:
					logger.warning("No download button found on the Suno song page.")
					return BrowserDownloadResult(audio_path=None, metadata=page_meta)

			with page.expect_download(timeout=45000) as download_info:
				await page.locator(sel).first.click()
			download = await download_info.value

			filename = download.suggested_filename or "suno_track.mp3"
			save_path = target_dir / filename
			await download.save_as(str(save_path))
			logger.info("Suno download saved: %s", save_path)
			return BrowserDownloadResult(audio_path=save_path, metadata=page_meta)
		except PlaywrightTimeoutError:
			logger.warning("Download timed out on Suno page: %s", song_url)
		except Exception as exc:
			logger.exception("Suno browser download failed: %s", exc)
		finally:
			try:
				await context.storage_state(path=str(storage_state_path))
				logger.info("Saved Suno storage state to %s", storage_state_path)
			except Exception as exc:
				logger.warning("Failed to save Suno storage state: %s", exc)

			# Save cookies for next time
			try:
				cookies = await context.cookies()
				cookies_path.write_text(json.dumps(cookies), encoding="utf-8")
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
	return BrowserDownloadResult(audio_path=None)
