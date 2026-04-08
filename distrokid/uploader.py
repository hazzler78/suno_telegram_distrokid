from __future__ import annotations

import asyncio
import json
from datetime import date, timedelta
from pathlib import Path
from typing import Optional
import re

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

from config.settings import settings
from suno.metadata import TrackMetadata
from utils import get_logger

logger = get_logger(__name__)


DISTROKID_URL = "https://distrokid.com/"
DISTROKID_NEW_URL = "https://distrokid.com/new/"
DISTROKID_NEWRELEASE_URL = "https://distrokid.com/newrelease/"


async def _snapshot(page, name: str) -> None:
	try:
		ss_path = settings.paths.work_dir / f"distrokid_{name}.png"
		html_path = settings.paths.work_dir / f"distrokid_{name}.html"
		await page.screenshot(path=str(ss_path), full_page=True)
		html_path.write_text(await page.content(), encoding="utf-8")
		logger.info("Saved DistroKid debug artifacts: %s | %s", ss_path, html_path)
	except Exception as exc:
		logger.warning("Failed to write DistroKid snapshot '%s': %s", name, exc)


async def _goto_release_page_with_retry(page, retries: int = 3) -> None:
	last_exc: Optional[Exception] = None
	for attempt in range(1, retries + 1):
		for url in (DISTROKID_NEW_URL, DISTROKID_NEWRELEASE_URL):
			try:
				await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
				await page.wait_for_timeout(1000)
				return
			except Exception as exc:
				last_exc = exc
				logger.warning("DistroKid navigate attempt %s/%s to %s failed: %s", attempt, retries, url, exc)
		await asyncio.sleep(2)
	if last_exc:
		raise last_exc


async def _find_locator_across_frames(page, selectors: list[str]):
	frames = [page.main_frame, *page.frames]
	for frame in frames:
		for selector in selectors:
			loc = frame.locator(selector)
			if await loc.count() > 0:
				return loc.first, selector, frame.url
	return None, None, None


async def _find_visible_locator_across_frames(page, selectors: list[str]):
	frames = [page.main_frame, *page.frames]
	for frame in frames:
		for selector in selectors:
			loc = frame.locator(selector)
			count = await loc.count()
			for idx in range(count):
				candidate = loc.nth(idx)
				try:
					if await candidate.is_visible():
						return candidate, selector, frame.url
				except Exception:
					continue
	return None, None, None


async def _click_sign_in_if_present(page) -> bool:
	sign_in_selectors = [
		"a:has-text('Sign in')",
		"a:has-text('Log in')",
		"button:has-text('Sign in')",
		"button:has-text('Log in')",
		"[data-testid*='sign-in']",
		"[data-testid*='login']",
	]
	locator, selector, frame_url = await _find_visible_locator_across_frames(page, sign_in_selectors)
	if not locator:
		return False
	try:
		await locator.click(timeout=10_000)
		logger.info("Clicked sign-in entry via selector '%s' (frame=%s)", selector, frame_url)
		await page.wait_for_timeout(1500)
		await _snapshot(page, "after_sign_in_click")
		return True
	except Exception as exc:
		logger.warning("Sign-in click failed for selector '%s': %s", selector, exc)
		return False


async def _wait_for_release_form(page, timeout_ms: int = 45_000) -> bool:
	check_selectors = [
		"#albumTitleInput",
		"#artistName",
		"input[id^='title_']",
		"input[name^='title_']",
		"#artwork",
		"#js-track-upload-1",
	]
	deadline = asyncio.get_event_loop().time() + (timeout_ms / 1000.0)
	while asyncio.get_event_loop().time() < deadline:
		locator, _, _ = await _find_locator_across_frames(page, check_selectors)
		if locator:
			return True
		await page.wait_for_timeout(1000)
	return False


async def _wait_for_manual_release_form(page, timeout_ms: int = 240_000) -> bool:
	logger.info(
		"Waiting for manual DistroKid login/CAPTCHA completion (up to %ss)...",
		timeout_ms // 1000,
	)
	deadline = asyncio.get_event_loop().time() + (timeout_ms / 1000.0)
	while asyncio.get_event_loop().time() < deadline:
		if await _wait_for_release_form(page, timeout_ms=1000):
			return True
		await page.wait_for_timeout(1000)
	return False


async def _ensure_release_context(page, retries: int = 3) -> bool:
	for attempt in range(1, retries + 1):
		current_url = page.url or ""
		if re.search(r"/(new|newrelease)", current_url, re.IGNORECASE):
			if await _wait_for_release_form(page, timeout_ms=5000):
				return True
		if re.search(r"/mymusic|/dashboard|/home", current_url, re.IGNORECASE):
			logger.info("Detected post-login redirect to %s; forcing release page.", current_url)
		try:
			await _goto_release_page_with_retry(page, retries=1)
			await page.wait_for_timeout(1500)
			if await _wait_for_release_form(page, timeout_ms=8000):
				return True
		except Exception as exc:
			logger.warning("Release context ensure attempt %s/%s failed: %s", attempt, retries, exc)
		await asyncio.sleep(1)
	return False


async def _is_homepage_signin_context(page) -> bool:
	# DistroKid sometimes serves homepage/signin modal even when /newrelease is requested.
	signin_markers = [
		"#signInButtonFrontPage",
		".signinWrapper",
		"#inputSigninEmail",
		"#signinForm",
	]
	release_markers = [
		"#albumTitleInput",
		"#artistName",
		"input[id^='title_']",
		"input[name^='title_']",
		"#artwork",
		"#js-track-upload-1",
	]
	signin_loc, _, _ = await _find_locator_across_frames(page, signin_markers)
	release_loc, _, _ = await _find_locator_across_frames(page, release_markers)
	return bool(signin_loc and not release_loc)


async def _try_direct_signin(page) -> bool:
	email_loc, _, _ = await _find_visible_locator_across_frames(page, ["#inputSigninEmail", "input[name=inputSigninEmail]"])
	pass_loc, _, _ = await _find_visible_locator_across_frames(page, ["#inputSigninPassword", "input[name=inputSigninPassword]"])
	if not email_loc or not pass_loc:
		return False
	try:
		await email_loc.fill(settings.distrokid_email or "")
		await pass_loc.fill(settings.distrokid_password or "")
		submit_loc, _, _ = await _find_visible_locator_across_frames(
			page,
			[
				"input#signinButton[value='Sign in']",
				"input.sign-in-button-round#signinButton",
				"button:has-text('Sign in')",
				"button:has-text('Log in')",
			],
		)
		if submit_loc:
			await submit_loc.click()
		else:
			await pass_loc.press("Enter")
		logger.info("Submitted DistroKid sign-in form via direct homepage login fields.")
		await page.wait_for_timeout(2500)
		await _snapshot(page, "after_direct_signin_submit")
		return True
	except Exception as exc:
		logger.warning("Direct sign-in attempt failed: %s", exc)
		return False


async def _signin_via_signin_page(page) -> bool:
	try:
		await page.goto("https://distrokid.com/signin/", wait_until="domcontentloaded", timeout=60_000)
		await page.wait_for_timeout(1200)
		await _snapshot(page, "signin_page")

		# Some versions render signin fields hidden until the top "Sign in" control is clicked.
		email_loc, _, _ = await _find_visible_locator_across_frames(page, ["#inputSigninEmail", "input[name=inputSigninEmail]"])
		pass_loc, _, _ = await _find_visible_locator_across_frames(page, ["#inputSigninPassword", "input[name=inputSigninPassword]"])
		if not (email_loc and pass_loc):
			trigger_loc, _, _ = await _find_visible_locator_across_frames(
				page,
				[
					"#signInButtonFrontPage",
					".signinWrapper",
					"div:has-text('Sign in')",
					"a:has-text('Sign in')",
				],
			)
			if trigger_loc:
				await trigger_loc.click()
				await page.wait_for_timeout(1200)
				await _snapshot(page, "signin_page_after_open_click")

		email_loc, _, _ = await _find_visible_locator_across_frames(page, ["#inputSigninEmail", "input[name=inputSigninEmail]"])
		pass_loc, _, _ = await _find_visible_locator_across_frames(page, ["#inputSigninPassword", "input[name=inputSigninPassword]"])
		if not email_loc or not pass_loc:
			logger.warning("Signin page loaded but email/password fields were not found.")
			await _snapshot(page, "signin_page_missing_fields")
			return False

		await email_loc.fill(settings.distrokid_email or "")
		await pass_loc.fill(settings.distrokid_password or "")

		submit_loc, _, _ = await _find_visible_locator_across_frames(
			page,
			[
				"input#signinButton[value='Sign in']",
				"input.sign-in-button-round#signinButton",
				"button:has-text('Sign in')",
				"button:has-text('Log in')",
				"input[type=submit][value='Sign in']",
			],
		)
		if submit_loc:
			await submit_loc.click()
		else:
			await pass_loc.press("Enter")
		await page.wait_for_timeout(2500)
		await _snapshot(page, "signin_page_after_submit")
		return True
	except Exception as exc:
		logger.warning("Signin via /signin page failed: %s", exc)
		await _snapshot(page, "signin_page_error")
		return False


async def _set_input_file(page, selectors: list[str], file_path: Path, label: str) -> bool:
	locator, selector, frame_url = await _find_visible_locator_across_frames(page, selectors)
	if not locator:
		# Fallback to hidden file input; set_input_files often works even when hidden.
		locator, selector, frame_url = await _find_locator_across_frames(page, selectors)
	if not locator:
		logger.warning("Could not find %s input element", label)
		return False
	await locator.set_input_files(str(file_path))
	logger.info("Attached %s via selector '%s' (frame=%s)", label, selector, frame_url)
	return True


async def _fill_input(page, selectors: list[str], value: str, field_name: str) -> bool:
	locator, selector, frame_url = await _find_visible_locator_across_frames(page, selectors)
	if not locator:
		await _snapshot(page, f"missing_{field_name}")
		logger.warning("Could not find %s input. Selectors tried: %s", field_name, selectors)
		return False
	await locator.fill(value)
	logger.info("Filled %s via selector '%s' (frame=%s)", field_name, selector, frame_url)
	return True


async def _set_artist_field(page, artist_value: str) -> bool:
	locator, selector, frame_url = await _find_visible_locator_across_frames(
		page,
		[
			"#artistName",
			"input[name='bandname']",
			"select[name='bandname']",
			"input[id*='artistName']",
		],
	)
	if not locator:
		await _snapshot(page, "missing_artist")
		logger.warning("Could not find artist field.")
		return False
	try:
		tag = await locator.evaluate("el => el.tagName.toLowerCase()")
		if tag == "select":
			options = await locator.evaluate(
				"""el => Array.from(el.options).map(o => ({value: o.value, text: (o.textContent || '').trim()}))"""
			)
			target = (artist_value or "").strip().lower()
			chosen_value = ""
			for opt in options:
				text = (opt.get("text") or "").strip().lower()
				val = (opt.get("value") or "").strip()
				if target and target == text and val:
					chosen_value = val
					break
			if not chosen_value and options:
				# Fallback: try partial text match.
				for opt in options:
					text = (opt.get("text") or "").strip().lower()
					val = (opt.get("value") or "").strip()
					if target and target in text and val:
						chosen_value = val
						break
			if not chosen_value:
				# Leave the existing selected option if no exact match.
				logger.warning("Artist select did not contain '%s'; keeping current value.", artist_value)
				return True
			await locator.select_option(value=chosen_value)
			await locator.dispatch_event("change")
			logger.info("Selected artist via '%s' value '%s' (frame=%s)", selector, chosen_value, frame_url)
			return True
		await locator.fill(artist_value)
		logger.info("Filled artist via selector '%s' (frame=%s)", selector, frame_url)
		return True
	except Exception as exc:
		logger.warning("Failed to set artist field: %s", exc)
		return False


async def _fill_input_if_present(page, selectors: list[str], value: str, field_name: str) -> bool:
	locator, selector, frame_url = await _find_visible_locator_across_frames(page, selectors)
	if not locator:
		return False
	await locator.fill(value)
	logger.info("Filled optional %s via selector '%s' (frame=%s)", field_name, selector, frame_url)
	return True


async def _set_store_checkbox(page, selector: str, checked: bool, label: str) -> bool:
	locator, used_selector, frame_url = await _find_locator_across_frames(page, [selector])
	if not locator:
		logger.warning("Store checkbox not found for %s (%s)", label, selector)
		return False
	try:
		is_checked = await locator.is_checked()
		if is_checked != checked:
			await locator.set_checked(checked, force=True)
			# Trigger JS listeners on this dynamic form.
			await locator.dispatch_event("change")
			await locator.dispatch_event("click")
		logger.info("Set store '%s' checked=%s via '%s' (frame=%s)", label, checked, used_selector, frame_url)
		return True
	except Exception as exc:
		logger.warning("Failed to set store checkbox for %s: %s", label, exc)
		return False


async def _enforce_store_preferences(page) -> None:
	# DistroKid client-side scripts can auto-toggle stores after metadata changes.
	await _set_store_checkbox(page, "#chkapplemusic", checked=False, label="Apple Music")
	await _set_store_checkbox(page, "#chkitunes", checked=False, label="iTunes")

	# Re-assert via JS in case custom handlers flip them back.
	try:
		await page.evaluate(
			"""
			() => {
				const ids = ['chkapplemusic', 'chkitunes'];
				for (const id of ids) {
					const el = document.getElementById(id);
					if (el && el.checked) {
						el.checked = false;
						el.dispatchEvent(new Event('change', { bubbles: true }));
						el.dispatchEvent(new Event('click', { bubbles: true }));
					}
				}
			}
			"""
		)
	except Exception as exc:
		logger.warning("JS store preference enforcement failed: %s", exc)


async def _select_artist_mapping_defaults(page) -> None:
	# DistroKid often requires explicit radio selection in artist mapping sections.
	# Prefer "No / first release" variants to avoid ambiguous matches.
	preferred_radio_ids = [
		"spotifyNoArtistIDFirst",
		"spotifyArtistIDFirst",
		"js-spotify-artist-id-zero-matches-new",
		"googleNoArtistIDFirst",
		"googleArtistIDFirst",
		"js-google-artist-id-zero-matches-new",
		"appleNoArtistIDFirst",
		"appleArtistIDFirst",
		"js-apple-artist-id-zero-matches-new",
		"js-instagramProfile-artist-id-zero-matches-new",
		"js-facebookProfile-artist-id-zero-matches-new",
	]
	for radio_id in preferred_radio_ids:
		locator, _, _ = await _find_visible_locator_across_frames(page, [f"#{radio_id}"])
		if not locator:
			continue
		try:
			checked = await locator.is_checked()
			if not checked:
				await locator.set_checked(True, force=True)
				await locator.dispatch_event("change")
				await locator.dispatch_event("click")
			logger.info("Selected artist mapping radio: #%s", radio_id)
		except Exception as exc:
			logger.warning("Failed selecting artist mapping radio #%s: %s", radio_id, exc)


async def _set_release_date(page) -> bool:
	# Pick a safe near-future release date within DistroKid allowed range.
	target = (date.today() + timedelta(days=7)).isoformat()
	return await _fill_input_if_present(
		page,
		["#release-date-dp", "input[name='releaseDate']", "input[type='date'][name='releaseDate']"],
		target,
		"release_date",
	)


async def _set_primary_genre(page, metadata_genre: str) -> bool:
	locator, selector, frame_url = await _find_visible_locator_across_frames(
		page,
		["#genrePrimary", "select[name='genre1']", "select#genrePrimary"],
	)
	if not locator:
		logger.warning("Primary genre selector not found.")
		return False

	# First try by visible label text, then fallback to first valid non-empty option.
	genre_text = (metadata_genre or "").strip().lower()
	try:
		options = await locator.evaluate(
			"""el => Array.from(el.options).map(o => ({value: o.value, text: (o.textContent || '').trim()}))"""
		)
		chosen_value = ""
		if genre_text:
			for opt in options:
				text = (opt.get("text") or "").lower()
				if genre_text in text or text in genre_text:
					chosen_value = opt.get("value") or ""
					if chosen_value:
						break
		if not chosen_value:
			for opt in options:
				value = (opt.get("value") or "").strip()
				text = (opt.get("text") or "").strip().lower()
				if value and "select a genre" not in text:
					chosen_value = value
					break
		if not chosen_value:
			logger.warning("No valid primary genre option found.")
			return False
		await locator.select_option(value=chosen_value)
		await locator.dispatch_event("change")
		await locator.dispatch_event("blur")
		logger.info("Selected primary genre value '%s' via '%s' (frame=%s)", chosen_value, selector, frame_url)
		return True
	except Exception as exc:
		logger.warning("Failed to set primary genre: %s", exc)
		return False


async def _set_primary_subgenre(page) -> bool:
	locator, selector, frame_url = await _find_visible_locator_across_frames(
		page,
		["#subGenrePrimary", "select[name='subgenre1']"],
	)
	if not locator:
		# Not all genres require a subgenre.
		return False
	try:
		options = await locator.evaluate(
			"""el => Array.from(el.options).map(o => ({value: o.value, text: (o.textContent || '').trim()}))"""
		)
		chosen_value = ""
		for opt in options:
			value = (opt.get("value") or "").strip()
			text = (opt.get("text") or "").strip().lower()
			if value and "select" not in text:
				chosen_value = value
				break
		if not chosen_value:
			return False
		await locator.select_option(value=chosen_value)
		await locator.dispatch_event("change")
		await locator.dispatch_event("blur")
		logger.info("Selected primary subgenre value '%s' via '%s' (frame=%s)", chosen_value, selector, frame_url)
		return True
	except Exception as exc:
		logger.warning("Failed to set primary subgenre: %s", exc)
		return False


async def _fill_songwriter_real_name(page, first_name: str, last_name: str) -> bool:
	first_loc, first_sel, first_frame = await _find_visible_locator_across_frames(
		page,
		[
			".songwriter_real_name_first[tracknum='1']",
			"input[name='songwriter_real_name_first1']",
			".songwriter_real_name_first",
		],
	)
	last_loc, last_sel, last_frame = await _find_visible_locator_across_frames(
		page,
		[
			".songwriter_real_name_last[tracknum='1']",
			"input[name='songwriter_real_name_last1']",
			".songwriter_real_name_last",
		],
	)
	if not first_loc or not last_loc:
		logger.warning("Songwriter real-name inputs were not visible.")
		return False
	try:
		await first_loc.fill(first_name)
		await first_loc.dispatch_event("change")
		await last_loc.fill(last_name)
		await last_loc.dispatch_event("change")
		logger.info(
			"Filled songwriter name via '%s'/'%s' (frames=%s/%s)",
			first_sel,
			last_sel,
			first_frame,
			last_frame,
		)
		return True
	except Exception as exc:
		logger.warning("Failed to fill songwriter real-name fields: %s", exc)
		return False


async def _check_mandatory_checkboxes(page) -> int:
	# DistroKid requires checking "Important checkboxes (mandatory)" before submit.
	try:
		container = page.locator("#checkboxtimes")
		if await container.count() == 0:
			logger.warning("Mandatory checkbox container '#checkboxtimes' not found.")
			return 0
		boxes = container.locator("input[type='checkbox']")
		count = await boxes.count()
		changed = 0
		for idx in range(count):
			box = boxes.nth(idx)
			try:
				if not await box.is_visible():
					continue
				if not await box.is_checked():
					await box.set_checked(True, force=True)
					changed += 1
			except Exception:
				continue
		logger.info("Checked %s mandatory agreement checkboxes.", changed)
		return changed
	except Exception as exc:
		logger.warning("Failed checking mandatory agreement checkboxes: %s", exc)
		return 0


async def _is_submit_success(page) -> bool:
	url = page.url or ""
	if re.search(r"/done|/success|/thanks|/receipt|/complete", url, re.IGNORECASE):
		return True

	# Stronger signals than generic "success" words.
	success_selectors = [
		"text=/release has been submitted/i",
		"text=/your release has been submitted/i",
		"text=/we.?re processing your release/i",
		"text=/submission received/i",
	]
	for sel in success_selectors:
		try:
			if await page.locator(sel).count() > 0:
				return True
		except Exception:
			continue
	return False


async def _choose_original_audio_if_prompted(page) -> bool:
	# Mixea upsell flow: "Choose a version" -> "Use my originals" -> continue/confirm.
	choice_selectors = [
		"input[type='radio'][value*='original' i]",
		"label:has-text('Use my originals')",
		"text=Use my originals",
	]
	choice_clicked = False
	for sel in choice_selectors:
		try:
			loc, used, frame_url = await _find_visible_locator_across_frames(page, [sel])
			if not loc:
				continue
			await loc.click()
			logger.info("Selected 'Use my originals' via '%s' (frame=%s)", used, frame_url)
			choice_clicked = True
			await page.wait_for_timeout(600)
			break
		except Exception:
			continue

	confirm_selectors = [
		"button:has-text('Continue')",
		"button:has-text('Confirm')",
		"button:has-text('Done')",
		"button:has-text('Use my originals')",
		"input[type='button'][value='Continue']",
		"input[type='submit'][value='Continue']",
	]
	for sel in confirm_selectors:
		try:
			loc, used, frame_url = await _find_visible_locator_across_frames(page, [sel])
			if not loc:
				continue
			await loc.click()
			logger.info("Confirmed original-audio choice via '%s' (frame=%s)", used, frame_url)
			await page.wait_for_timeout(1200)
			await _snapshot(page, "after_original_audio_choice")
			return True
		except Exception:
			continue

	if choice_clicked:
		await _snapshot(page, "after_original_audio_choice_only")
		return True

	selectors = [
		"button:has-text('Original audio')",
		"a:has-text('Original audio')",
		"button:has-text('Keep original')",
		"a:has-text('Keep original')",
		"button:has-text('No thanks')",
		"a:has-text('No thanks')",
		"button:has-text('Skip')",
	]
	for sel in selectors:
		try:
			loc, used, frame_url = await _find_visible_locator_across_frames(page, [sel])
			if not loc:
				continue
			await loc.click()
			logger.info("Selected original-audio option via '%s' (frame=%s)", used, frame_url)
			await page.wait_for_timeout(1500)
			await _snapshot(page, "after_original_audio_choice")
			return True
		except Exception:
			continue
	return False


async def _has_visible_validation_errors(page) -> bool:
	error_selectors = [
		"#errors",
		".error",
		".errorlist",
		".validation-error",
		"text=/make sure you did everything right/i",
		"text=/please select/i",
		"text=/missing/i",
	]
	for sel in error_selectors:
		try:
			loc = page.locator(sel)
			if await loc.count() > 0 and await loc.first.is_visible():
				return True
		except Exception:
			continue
	return False


async def _wait_for_submit_outcome(page, timeout_ms: int = 90_000) -> bool:
	"""
	After clicking submit, DistroKid can take a while before showing a success URL/message.
	Poll for success and keep handling optional enhancement prompts.
	"""
	deadline = asyncio.get_event_loop().time() + (timeout_ms / 1000.0)
	while asyncio.get_event_loop().time() < deadline:
		try:
			if await _is_submit_success(page):
				return True
			if await _choose_original_audio_if_prompted(page):
				# If upsell was handled and no visible validation errors remain,
				# treat this as successful continuation.
				await page.wait_for_timeout(2000)
				if not await _has_visible_validation_errors(page):
					return True
			# If form validation errors are visible, no need to wait longer.
			if await _has_visible_validation_errors(page):
				return False
		except Exception:
			pass
		await page.wait_for_timeout(1500)
	return await _is_submit_success(page)


async def upload_release(audio_path: Path, cover_path: Path, metadata: TrackMetadata) -> bool:
	if not settings.distrokid_email or not settings.distrokid_password:
		logger.warning("DistroKid credentials missing; skipping upload step.")
		return False

	cookies_path = settings.paths.cookies_dir / "distrokid_cookies.json"
	storage_state_path = settings.paths.cookies_dir / "distrokid_storage_state.json"
	
	async with async_playwright() as p:
		browser = await p.chromium.launch(headless=not settings.debug)
		if storage_state_path.exists():
			context = await browser.new_context(
				accept_downloads=False,
				storage_state=str(storage_state_path),
			)
			logger.info("Loaded DistroKid storage state from %s", storage_state_path)
		else:
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
			await _goto_release_page_with_retry(page)
			await _snapshot(page, "newrelease_landing")

			# Some sessions land on homepage with embedded sign-in form instead of /login.
			if await _is_homepage_signin_context(page):
				logger.info("Detected homepage/signin context. Forcing /signin flow.")
				signed_in = await _signin_via_signin_page(page)
				if not signed_in:
					await _snapshot(page, "forced_signin_failed")
					return False
				try:
					await _goto_release_page_with_retry(page, retries=1)
					await page.wait_for_timeout(1500)
					await _snapshot(page, "after_forced_signin_newrelease")
				except Exception as exc:
					logger.warning("Failed to load release page after forced /signin flow: %s", exc)
					return False
			elif await _try_direct_signin(page):
				try:
					await _goto_release_page_with_retry(page, retries=1)
					await page.wait_for_timeout(1500)
					await _snapshot(page, "after_direct_signin_newrelease")
				except Exception as exc:
					logger.warning("Failed to load release page after direct sign-in: %s", exc)

			if re.search(r"/login", page.url, re.IGNORECASE):
				logger.info(
					"DistroKid requires login. Please accept cookies and sign in in the opened browser; "
					"waiting up to 3 minutes for release page to load..."
				)
				try:
					await page.wait_for_url(
						re.compile(r".*distrokid\.com/.*/(new|newrelease).*", re.IGNORECASE),
						timeout=180_000,
					)
					await _snapshot(page, "after_login")
				except Exception as te:
					logger.warning("DistroKid login did not complete in time: %s", te)
					await _snapshot(page, "login_timeout")
					return False

			await page.wait_for_load_state("domcontentloaded")
			await page.wait_for_timeout(2000)
			if not await _ensure_release_context(page, retries=2):
				logger.info("Initial release context check not ready; continuing with sign-in handling.")
			if not await _wait_for_release_form(page, timeout_ms=8000):
				# First try explicit /signin route (most reliable when homepage modal is hidden).
				signed_in = await _signin_via_signin_page(page)
				if signed_in:
					try:
						await _goto_release_page_with_retry(page, retries=1)
						await page.wait_for_timeout(2000)
						await _snapshot(page, "after_signin_page_newrelease")
					except Exception as exc:
						logger.warning("Failed to load release page after /signin flow: %s", exc)

				# Fallback: click sign-in button/link on current page and wait.
				if not signed_in:
					clicked_sign_in = await _click_sign_in_if_present(page)
					if clicked_sign_in and re.search(r"/login|/signin", page.url, re.IGNORECASE):
						logger.info("Redirected to login page after sign-in click; waiting for manual login completion.")
						try:
							await page.wait_for_url(
								re.compile(r".*distrokid\.com/.*/(new|newrelease).*", re.IGNORECASE),
								timeout=180_000,
							)
						except Exception as exc:
							logger.warning("Manual login wait timed out after sign-in click: %s", exc)
							await _snapshot(page, "login_timeout_after_sign_in_click")
							return False
				if not await _wait_for_release_form(page, timeout_ms=30_000):
					# In debug mode, allow manual completion of CAPTCHA/login.
					if settings.debug:
						await _snapshot(page, "awaiting_manual_login")
						manual_ok = await _wait_for_manual_release_form(page, timeout_ms=240_000)
						if manual_ok:
							await _snapshot(page, "manual_login_completed")
						else:
							await _snapshot(page, "manual_login_timeout")
							logger.warning("Manual login/CAPTCHA did not complete in time.")
							return False
					else:
						await _snapshot(page, "release_form_not_found")
						logger.warning("DistroKid release form not detected after sign-in handling.")
						return False
				if not await _ensure_release_context(page, retries=3):
					await _snapshot(page, "release_form_not_found")
					logger.warning("DistroKid release form not detected after sign-in handling.")
					return False
			await _snapshot(page, "before_form_fill")

			# User preference: keep Apple Music + iTunes unchecked.
			await _enforce_store_preferences(page)

			await _set_input_file(
				page,
				[
					"#artwork",
					"input[type=file][name=cover]",
					"input[type=file][accept*='image']",
					"input[type=file][id*='cover']",
				],
				cover_path,
				"cover",
			)

			await _set_input_file(
				page,
				[
					"#js-track-upload-1",
					"input.trackupload",
					"input[type=file][name=audio]",
					"input[type=file][accept*='audio']",
					"input[type=file][id*='audio']",
					"input[type=file][name*='song']",
				],
				audio_path,
				"audio",
			)

			ok_artist = await _set_artist_field(page, metadata.artist)
			ok_album_title = await _fill_input_if_present(
				page,
				[
					"#albumTitleInput",
					"input[name='albumtitle']",
				],
				metadata.title,
				"album_title",
			)
			ok_track_title = await _fill_input_if_present(
				page,
				[
					"input[id^='title_']",
					"input[name^='title_']",
					"input.uploadFileTitle",
					"input[placeholder*='Track 1 title']",
				],
				metadata.title,
				"track_title",
			)
			# Artist may already be pre-filled and locked on some DistroKid accounts.
			if not ok_artist:
				return False
			# At least one title input path must be filled.
			if not (ok_album_title or ok_track_title):
				await _snapshot(page, "missing_title_fields")
				return False

			_ = await _fill_input(
				page,
				[
					"input[name=genre]",
					"input#genre",
					"input[placeholder*='Genre']",
				],
				metadata.genre,
				"genre",
			)

			# Enforce again after dynamic form updates.
			await _enforce_store_preferences(page)
			await _set_primary_genre(page, metadata.genre)
			await _set_primary_subgenre(page)
			await _set_release_date(page)
			await _select_artist_mapping_defaults(page)
			await _fill_songwriter_real_name(page, first_name="Mikael", last_name="Soderberg")
			await _check_mandatory_checkboxes(page)

			await _snapshot(page, "filled")

			submit_selectors = [
				"#doneButton",
				"input#doneButton",
				"input[type=button][value='Done']",
				"input[type=button][value='Continue']",
				"button[type=submit]",
				"button:has-text('Done')",
				"button:has-text('Submit')",
			]
			for sel in submit_selectors:
				loc, _, _ = await _find_locator_across_frames(page, [sel])
				if loc:
					await loc.click()
					await page.wait_for_timeout(2000)
					await _snapshot(page, "after_submit_click")
					break
			else:
				await _snapshot(page, "missing_submit")
				return False

			try:
				# Network may stay active; don't rely solely on networkidle here.
				try:
					await page.wait_for_load_state("domcontentloaded", timeout=15_000)
				except Exception:
					pass
				if await _wait_for_submit_outcome(page, timeout_ms=90_000):
					logger.info("DistroKid submission appears successful: %s", page.url)
					return True
			except PlaywrightTimeoutError:
				pass

			# One safe retry for transient network/submit glitches before failing.
			if not await _has_visible_validation_errors(page):
				logger.info("Retrying DistroKid submit once due to inconclusive outcome.")
				for sel in submit_selectors:
					loc, _, _ = await _find_locator_across_frames(page, [sel])
					if loc:
						try:
							await loc.click()
							await page.wait_for_timeout(1800)
							await _snapshot(page, "after_submit_retry_click")
							break
						except Exception:
							continue
				if await _wait_for_submit_outcome(page, timeout_ms=60_000):
					logger.info("DistroKid submission appears successful after retry: %s", page.url)
					return True

			await _snapshot(page, "submit_failed")
			logger.warning("DistroKid submission may have failed; inspect distrokid_submit_failed artifacts.")
			return False
		except PlaywrightTimeoutError as te:
			logger.warning("DistroKid flow timeout: %s", te)
			await _snapshot(page, "flow_timeout")
			return False
		except Exception as exc:
			logger.exception("DistroKid upload flow failed: %s", exc)
			await _snapshot(page, "flow_exception")
			return False
		finally:
			# Save full browser storage state for future runs (cookies + local/session storage)
			try:
				await context.storage_state(path=str(storage_state_path))
				logger.info("Saved DistroKid storage state to %s", storage_state_path)
			except Exception as exc:
				logger.warning("Failed to save DistroKid storage state: %s", exc)

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
			try:
				await context.close()
			except Exception:
				pass
			try:
				await browser.close()
			except Exception:
				pass
